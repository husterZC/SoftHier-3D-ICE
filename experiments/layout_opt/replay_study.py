#!/usr/bin/env python3
"""Replay the four measured layouts with a cooling override and fixed activity."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from experiments.layout_opt.compare import validate_comparison
from experiments.layout_opt.transforms import local, write_json
from experiments.layout_opt.run import base
from experiments.layout_opt.config import (CAMPAIGN as SOURCE_CAMPAIGN,
    REPLAY_CAMPAIGN, HEAT_TRANSFER_COEFFICIENT, LAYOUTS)
from experiments.llm_dram3d.opt_steps import opt_metadata
from experiments.thermal_replay.activity import digest, load_recording
from experiments.thermal_replay.record import record
import numpy as np

HERE = ROOT / "experiments/layout_opt"
NAMES = tuple(name for name, _ in LAYOUTS)
CAMPAIGN = REPLAY_CAMPAIGN
PROFILES = ("temperature_aware", "constant")
SERVER = ROOT / "3D-ICE/bin/3D-ICE-Server"


def prepare(campaign, source_results=None, heat_transfer_coefficient=HEAT_TRANSFER_COEFFICIENT):
    folder = local(HERE / "artifacts" / campaign)
    if folder.exists():
        raise ValueError("campaign already exists; use run to continue completed preparations")
    source_results = local(source_results or HERE / "results" / SOURCE_CAMPAIGN)
    if not np.isfinite(heat_transfer_coefficient) or heat_transfer_coefficient <= 0:
        raise ValueError("cooling coefficient must be positive and finite")
    folders = [source_results / name for name in NAMES]
    items = [json.loads((path / "metrics.json").read_text()) for path in folders]
    evidence = validate_comparison(items, folders)
    source_studies = [json.loads(local(ROOT / item["trial"] / "study.json").read_text()) for item in items]
    if any(study.get("thermal_server_sha256") != digest(SERVER) for study in source_studies):
        raise ValueError("native thermal binary changed since source simulations")
    source_study = source_studies[0]
    folder.mkdir(parents=True)
    implementation = folder / "implementation"
    implementation.mkdir()
    sources = [Path(__file__), HERE / "config.py", HERE / "run.py", HERE / "compare.py",
        ROOT / "experiments/llm_dram3d/opt_steps.py",
        *sorted((ROOT / "experiments/thermal_replay").glob("*.py"))]
    hashes = {}
    for path in sources:
        relative = path.relative_to(ROOT)
        target = implementation / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        hashes[str(relative)] = digest(path)
    study = dict(experiment="layout_opt_thermal_replay", campaign=campaign,
        case=source_study["cases"][0], **opt_metadata(source_study["opt_step"]), thermal_replay_till=1.,
        heat_transfer_coefficient_w_m2_k=heat_transfer_coefficient, allow_model_clamp=True,
        interval_ps=source_study["interval_ps"], thermal_step_ps=source_study["thermal_step_ps"],
        target_cells_per_die=source_study["target_cells"],
        stack_order=source_study["resolved_thermal_config"]["stack_order"], initial_temperature_k=300.,
        source_results=str(source_results.relative_to(ROOT)),
        profiles=list(PROFILES), dense_maps=True, factorization_threads=1,
        source_comparison_validation=evidence, server_sha256=digest(SERVER),
        implementation_sha256=hashes, layouts=[])
    reference = None
    timer_reference = None
    for item in items:
        source = local(ROOT / item["profiles"]["constant"]["run"])
        start, end = base.replay_bounds(source)
        if timer_reference is None:
            timer_reference = (start, end)
        elif (start, end) != timer_reference:
            raise ValueError("source timer bounds differ between layouts")
        recording = folder / "recordings" / item["name"]
        imported = record(source, recording, start=start, end=end, layers_per_cycle=1,
                          heat_transfer_coefficient=heat_transfer_coefficient)
        manifest, document, activity = load_recording(recording)
        if document["metadata"]["thermal_config"]["stack_order"] != study["stack_order"]:
            raise ValueError("source stack order differs")
        if manifest["power_interval_ps"] != study["interval_ps"]:
            raise ValueError("source power interval differs")
        arrays = [activity.edges, activity.dynamic, activity.leakage]
        if reference is None:
            reference = [array.copy() for array in arrays]
        elif any(not np.array_equal(a, b) for a, b in zip(arrays, reference)):
            raise ValueError("recorded dynamic or reference leakage differs between layouts")
        if manifest["thermal_variant"]["new_w_per_m2_k"] != heat_transfer_coefficient:
            raise ValueError("cooling override was not recorded")
        study["layouts"].append(dict(name=item["name"], layout=item["layout"],
            measurement=str((source_results / item["name"] / "metrics.json").relative_to(ROOT)),
            source_run=str(source.relative_to(ROOT)),
            recording=str(recording.relative_to(ROOT)),
            layer_start_ps=start, layer_end_ps=end, period_ps=end-start,
            source_binary_sha256=item["profiles"]["constant"]["binary_sha256"],
            source_preload_sha256=item["profiles"]["constant"]["preload_sha256"],
            recording_sha256=imported["sha256"], runs={}))
        print(f"Prepared {item['name']}: {end-start} ps period, h={heat_transfer_coefficient:g}", flush=True)
    study["recorded_activity_identical"] = True
    write_json(folder / "study.json", study)
    return folder, study


def check_complete(path, study, entry, profile):
    summary = json.loads((path / "summary.json").read_text())
    expected = (1_000_000_000_000-entry["layer_start_ps"]) // entry["period_ps"]
    required = dict(status="time_limit", profile=profile, slots=50_000,
        simulated_seconds=1., time_cap_seconds=1., allow_model_clamp=True,
        step_ps=study["thermal_step_ps"], slot_ps=study["interval_ps"],
        period_ps=entry["period_ps"], layer_start_ps=entry["layer_start_ps"],
        cycles_completed=expected, layers_completed=expected,
        factorization_threads=1, server_sha256=study["server_sha256"],
        recording_sha256=entry["recording_sha256"])
    for key, value in required.items():
        if summary.get(key) != value:
            raise ValueError(f"{path}: {key} differs from requested replay")
    stack = (path / "ice.stk").read_text()
    h = float(re.search(r"heat transfer coefficient\s+([^;]+)", stack)[1])*1e12
    if not np.isclose(h,study["heat_transfer_coefficient_w_m2_k"],rtol=0,atol=1e-8):
        raise ValueError("runtime cooling coefficient differs")
    return summary


def run(campaign, workers):
    folder = local(HERE / "artifacts" / campaign)
    study = json.loads((folder / "study.json").read_text())
    for filename, expected in study["implementation_sha256"].items():
        if digest(local(ROOT / filename)) != expected:
            raise ValueError("replay implementation changed after preparation")
    if digest(SERVER) != study["server_sha256"]:
        raise ValueError("native solver changed after preparation")
    (folder / "logs").mkdir(exist_ok=True)
    environment = dict(os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
                       PYTHONDONTWRITEBYTECODE="1")

    def execute(entry, profile):
        output = folder / "runs" / entry["name"] / profile
        if output.exists():
            # Finished jobs are resumable; incomplete histories are preserved.
            summary = check_complete(output, study, entry, profile)
        else:
            command = [sys.executable, "-m", "experiments.thermal_replay", "replay",
                "--recording", str(local(ROOT / entry["recording"])),
                "--output", str(output), "--profile", profile, "--max-seconds", "1",
                "--thermal-step", str(study["thermal_step_ps"] / 1e12),
                "--allow-model-clamp", "--dense-maps"]
            print(f"Starting {entry['name']}/{profile}", flush=True)
            with (folder / "logs" / f"{entry['name']}_{profile}.log").open("w") as log:
                subprocess.run(command, cwd=ROOT, env=environment, stdout=log,
                               stderr=subprocess.STDOUT, check=True)
            summary = check_complete(output, study, entry, profile)
        return dict(path=str(output.relative_to(ROOT)), summary=summary)

    errors = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {pool.submit(execute, entry, profile): (entry, profile)
                   for profile in PROFILES for entry in study["layouts"]}
        for future in as_completed(pending):
            entry, profile = pending[future]
            try:
                entry["runs"][profile] = future.result()
                print(f"Completed {entry['name']}/{profile}: 1 second", flush=True)
            except Exception as exc:
                errors.append(dict(name=entry["name"], profile=profile, error=str(exc)))
                print(f"Failed {entry['name']}/{profile}: {exc}", flush=True)
            write_json(folder / "study.json", study)
    write_json(folder / "execution.json", dict(complete=not errors, errors=errors))
    if errors:
        raise RuntimeError(f"{len(errors)} replay runs failed; see execution.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    parser.add_argument("--campaign", default=CAMPAIGN)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--source-results", type=Path, default=HERE / "results" / SOURCE_CAMPAIGN)
    parser.add_argument("--heat-transfer-coefficient", type=float, default=HEAT_TRANSFER_COEFFICIENT)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.campaign) or args.workers < 1:
        parser.error("invalid campaign name or worker count")
    if args.action == "prepare":
        prepare(args.campaign, args.source_results, args.heat_transfer_coefficient)
    else:
        run(args.campaign, args.workers)


if __name__ == "__main__":
    main()
