"""Prepare a failed pilot's verified capture for replay without rerunning B8.

Measured workflows always start fresh. Preparation archives the failed replay
and original provenance; it does not start a benchmark or rewrite capture data.
"""

import ast
import copy
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import shutil
import time

from . import replay_overhead as benchmark
from .replay_analysis import require, validate_capture
from experiments.thermal_replay.activity import digest, write_json

RECOVERY_SOURCES = {
    "experiments/thermal_replay/ice.py", "experiments/thermal_replay/replay.py",
    "experiments/thermal_replay/README.md",
    "experiments/closed_loop_overhead/replay_overhead.py",
    "experiments/closed_loop_overhead/replay_analysis.py",
    "experiments/closed_loop_overhead/replay_recovery.py",
    "experiments/closed_loop_overhead/test_replay_overhead.py",
    "experiments/closed_loop_overhead/README.md",
}


def check_scope(directory, manifest, reference, row):
    require(manifest.get("experiment") == "capture_replay_overhead_v1"
            and manifest["status"] in ("failed", "interrupted"), "recovery requires a failed capture/replay study")
    require(row["id"] == "pilot" and row["block"] == 0 and row["pilot"] is True
            and row["status"] in ("failed", "interrupted"), "only a failed pilot can reuse its capture")
    require(manifest["schedule"][0] == dict(id="pilot", block=0, pilot=True), "unexpected pilot schedule")
    require(not any((directory / "runs" / item["id"]).exists()
                    for item in manifest["schedule"] if not item["pilot"]),
            "pilot recovery must precede all measured workflows")
    identity = dict(directory=str(reference["directory"]), run=str(reference["source"]),
                    input_sha256=reference["input_sha256"])
    require(manifest["reference"] == identity
            and manifest["architectural_signature"] == reference["manifest"]["architectural_signature"],
            "recovery requires unchanged reference inputs and native binaries")
    old, new = manifest["source_sha256"], reference["source_sha256"]
    changed = {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
    require(changed <= RECOVERY_SOURCES,
            f"capture-related or unrelated sources changed: {sorted(changed - RECOVERY_SOURCES)}")
    for relative, checksum in old.items():
        require(digest(directory / "source_snapshot" / relative) == checksum,
                f"original source snapshot changed: {relative}")
    relative = "experiments/closed_loop_overhead/replay_overhead.py"
    def definitions(path):
        return {node.name: ast.dump(node) for node in ast.parse(path.read_text()).body
                if isinstance(node, ast.FunctionDef)}
    before = definitions(directory / "source_snapshot" / relative)
    after = definitions(benchmark.ROOT / relative)
    for name in ("capture_setup", "replay_command", "settings_for"):
        require(before[name] == after[name], f"recovery requires unchanged {name}")
    return sorted(changed)


def prepare(study):
    directory = Path(study).resolve()
    with (directory.parent / ".runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = json.loads((directory / "study.json").read_text())
        folder = directory / "runs/pilot"
        row = json.loads((folder / "measurement.json").read_text())
        reference = benchmark.load_reference(manifest["reference"]["directory"])
        changed = check_scope(directory, manifest, reference, row)
        benchmark.verify_identities(reference)
        capture, _ = validate_capture(folder, row, manifest)
        require(capture["end_ns"] < time.monotonic_ns(), "capture timestamps are from a different host clock")
        workload = reference["workload"]
        validation = benchmark.runner.parse_timing((folder / "capture/logs/simulator.log").read_text(),
            workload["cases"][benchmark.runner.CASE]["manifest"],
            benchmark.runner.load_sdk_report(workload["source"]), 16, 1)
        require(validation == row["validation"], "saved numerical validation differs from the simulator log")
        effective = benchmark.runner.effective_configuration(folder / "capture", workload["dramsys_path"], True)
        require(effective == row["effective_capture_configuration"], "effective capture configuration changed")
        # Hash the frozen capture before moving any failed replay outputs.
        evidence = [p for name in ("capture", "recording") for p in (folder / name).rglob("*") if p.is_file()]
        hashes = {str(p.relative_to(folder)): digest(p) for p in sorted(evidence)}
        archive = directory / "recoveries" / f"pilot_{time.time_ns()}"
        archive.mkdir(parents=True)
        shutil.copy2(directory / "study.json", archive / "study.json")
        shutil.copy2(folder / "measurement.json", archive / "measurement.json")
        shutil.copytree(directory / "source_snapshot", archive / "source_snapshot")
        for name in ("replay", "replay_timing", "replay.log", "replay.resources.json"):
            path = folder / name
            if path.exists():
                path.rename(archive / name)
        for relative in reference["source_sha256"]:
            destination = directory / "source_snapshot" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(benchmark.ROOT / relative, destination)
        recovery = dict(archive=str(archive.relative_to(directory)),
                        prepared_utc=datetime.now(timezone.utc).isoformat(), changed_sources=changed,
                        reason="retry pilot replay after startup failure; keep its validated capture",
                        original_measurement_sha256=digest(archive / "measurement.json"))
        updated = copy.deepcopy(row)
        for key in ("error", "replay", "combined", "artifact_sha256", "replay_command"):
            updated.pop(key, None)
        updated.update(status="capture_ready", capture_artifact_sha256=hashes,
                       reuse_capture=dict(recovery, original_source_sha256=row.get("reuse_capture", {}).get(
                           "original_source_sha256", manifest["source_sha256"])))
        manifest.setdefault("recoveries", []).append(recovery)
        manifest.update(status="prepared", source_sha256=reference["source_sha256"])
        write_json(folder / "measurement.json", updated)
        write_json(directory / "study.json", manifest)
        print(f"Pilot capture validated and preserved: {folder / 'capture'}\n"
              f"Failed replay and original sources archived: {archive}\n"
              "Ready for run --resume; no simulation started. Measured workflows will use fresh captures.", flush=True)
        return recovery
