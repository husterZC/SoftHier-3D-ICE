"""Measure one SoftHier capture plus a temperature-aware thermal replay.

The full benchmark is manual: one pilot workflow, then measured workflows.
No native builds or changes to the reference study are performed.
"""

import argparse
import copy
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import time

from . import runner, backend
from .analyze import read_events
from .replay_analysis import analyze, combine, require, summarize_capture, summarize_replay, validate_capture
from experiments.thermal_replay.activity import digest, load_contract, load_recording, write_json
from experiments.thermal_replay.layout import Layout
from experiments.thermal_replay.record import record
from Interface_scripts.system_contract import initial_temperature_c, thermal_components
from Interface_scripts.thermal_manifest import copy_inputs

ROOT, HERE = runner.ROOT, runner.HERE
DEFAULT_REFERENCE = HERE / "results/b8_overhead_optimized"
DEFAULT_PREFIX = "b8_replay_50um_1s"


def load_reference(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "study.json").read_text())
    settings = manifest["settings"]
    selected_backend = backend.from_settings(settings)
    require(manifest["status"] == "complete" and settings["case"] == "decode_b8_kv1024"
            and settings["profile"] == "temperature_aware" and settings["power_accounting_timing"]
            and settings["coupling_mode"] == "optimized", "requires a completed optimized B8 study with inline timing")
    require(manifest["configurations"].get("closed_loop_50") == 50
            and settings["interval_ps"] == 20_000_000
            and math.isclose(settings["thermal_step_seconds"], 2e-5, abs_tol=1e-15)
            and settings["thermal_threads"] == selected_backend["solver_threads"],
            "reference must use the 50 µm mesh and 20 µs steps with matching solver threads")
    selected = next((item for item in manifest["schedule"]
                     if not item["pilot"] and item["configuration"] == "closed_loop_50"), None)
    require(selected is not None, "reference has no measured 50 µm case")
    source = directory / "runs" / selected["id"]
    row = json.loads((source / "measurement.json").read_text())
    require(row["status"] == "complete" and row["validation"]["status"] == "PASS"
            and row["timing"]["native_cells"] == row["mesh"]["total_cells"] == 67139,
            "reference measurement is incomplete or has the wrong mesh")
    workload = json.loads((directory / "workload_reference.json").read_text())
    case = workload["cases"][runner.CASE]
    for field in ("binary", "preload"):
        require(digest(case[field]) == manifest[field + "_sha256"] == case[field + "_sha256"],
                f"reference {field} changed")
    require(workload["repeats"] == 1 and workload["seed"] == settings["seed"]
            and workload["layer"] == settings["layer"], "reference workload does not match study settings")
    runner.compatible_workload(workload, settings)
    document = load_contract(source / "generated/system_config.json")
    layout = Layout(source / "generated/3dice", document)
    inputs = [directory / "study.json", directory / "workload_reference.json", source / "measurement.json",
              source / "generated/system_config.json", source / "generated/3dice/ice.stk", *layout.inputs,
              Path(workload["arch"]), Path(workload["thermal_config"]), Path(workload["dram_power_config"]),
              Path(case["binary"]), Path(case["preload"])]
    # A newer source checkout may be used for instrumentation, but timing must
    # use the very same optimized native binaries as the reference study.
    for relative, checksum in manifest["native_artifact_sha256"].items():
        p = ROOT / relative
        require(p.is_file() and digest(p) == checksum, f"reference native artifact changed: {relative}")
        inputs.append(p)
    for executable in ("server", "client"):
        relative = str(Path(selected_backend[executable]).relative_to(ROOT))
        require(relative in manifest["native_artifact_sha256"], f"reference does not pin the selected {executable}")
    checksum = manifest["native_artifact_sha256"][str(Path(selected_backend["server"]).relative_to(ROOT))]
    if "thermal_backend" in settings:
        require(selected_backend == backend.selection(selected_backend["dice_dir"], selected_backend["build_target"],
            selected_backend["solver_type"], selected_backend["solver_threads"], selected_backend["solver_tolerance"]),
            "reference backend configuration is inconsistent")
        actual = backend.verify_run(source, selected_backend, checksum)
        require(row.get("thermal_backend") == actual, "reference measurement solver differs from generated inputs")
        if selected_backend["build_target"] == "cpu":
            inputs.append(source / "generated/thermal_solver.json")
    inputs.extend(Path(workload["dramsys_path"]).rglob("*.json"))
    sources = sorted(set(runner.source_files(settings.get("thermal_backend")))
                     | set((ROOT / "experiments/thermal_replay").glob("*.py")))
    return dict(directory=directory, source=source, manifest=manifest, workload=workload, document=document,
                input_sha256={str(p.resolve()): digest(p) for p in sorted(set(inputs))},
                source_sha256={str(p.relative_to(ROOT)): digest(p) for p in sources})


def capture_setup(directory, workload, reference_source, *, binary=None, preload=None, timed=True):
    """Prepare the actual recording-only path; no thermal process is launched."""
    directory = Path(directory).resolve()
    for folder in ("generated/3dice", "state", "logs", "traces", "timing", "results/simulator"):
        (directory / folder).mkdir(parents=True, exist_ok=True)
    case = workload["cases"][runner.CASE]
    env = runner.environment(workload)
    env.update(SOFTHIER_POWER_PROFILE="constant", SOFTHIER_BINARY=str(binary or case["binary"]),
               SIMULATOR_PLATFORM=str(case["preload"] if preload is None else preload),
               SOFTHIER_RUN_CWD=str(directory / "results/simulator"),
               SYSTEM_CONFIG_FILE=str(directory / "generated/system_config.json"),
               CLO_POWER_ACCOUNTING_TIMING="1" if timed else "0")
    if timed:
        env["CLO_TIMING_DIR"] = str(directory / "timing")
    runner.execute(["bash", str(runner.PROVIDER), "export-system"], env, directory / "logs/export.log", 60)
    document = load_contract(env["SYSTEM_CONFIG_FILE"])
    expected = load_contract(Path(reference_source) / "generated/system_config.json")
    expected = copy.deepcopy(expected)
    expected["metadata"]["power_model_profile"] = "constant"
    for field in ("metadata", "geometry", "floorplan", "power_trace", "thermal_feedback", "thermal_stack"):
        require(document.get(field) == expected.get(field), f"capture configuration differs in {field}")
    inputs = Path(reference_source) / "generated/3dice"
    copy_inputs(inputs / "thermal_manifest.json", directory / "generated/3dice/thermal_manifest.json",
                inputs / "ice.stk", directory / "generated/3dice/ice.stk")
    updates = []
    for component in thermal_components(document):
        entry = dict(path=component["path"], temperature_c=initial_temperature_c(document))
        if component.get("voltage_v") is not None:
            entry["voltage_v"] = component["voltage_v"]
        updates.append(entry)
    config = directory / "generated/record_hook.json"
    write_json(config, dict(temperatures=updates))
    hook = ROOT / "experiments/thermal_replay/record_hook.py"
    wrapper = directory / "record_hook.sh"
    wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(hook)) + ' "$@"\n')
    wrapper.chmod(0o755)
    env.update(POWER_HOOK_EXECUTABLE=str(wrapper), POWER_HOOK_CONFIG_FILE=str(config),
               POWER_HOOK_REQUEST_FILE=str(directory / "state/request.json"),
               POWER_HOOK_RESPONSE_FILE=str(directory / "state/response.json"),
               POWER_HOOK_TRACE_FILE=str(directory / "traces/power_hook_trace.jsonl"),
               POWER_INTERVAL_PS="20000000")
    write_json(directory / "environment.json", {k: v for k, v in env.items()
               if k.startswith(("SOFTHIER_", "SIMULATOR_", "POWER_HOOK_", "CLO_")) or k == "POWER_INTERVAL_PS"})
    return env


def replay_command(recording, output, settings):
    return [sys.executable, "-m", "experiments.thermal_replay", "replay", "--recording", str(recording),
            "--output", str(output), "--profile", "temperature_aware",
            "--thermal-step", f"{settings['interval_ps'] * 1e-12:.12g}",
            "--max-seconds", f"{settings['replay_simulated_ps'] * 1e-12:.12g}",
            *backend.replay_arguments(backend.from_settings(settings))]


def workflow(directory, item, reference, settings, reuse_capture=None):
    require(reuse_capture is None or (item["pilot"] and reuse_capture.get("reuse_capture")),
            "only an explicitly recovered pilot may reuse a capture")
    directory.mkdir(parents=True, exist_ok=reuse_capture is not None)
    result = copy.deepcopy(reuse_capture) if reuse_capture is not None else dict(
        item, workflow_start_ns=time.monotonic_ns(), loadavg_start=os.getloadavg())
    result.update(status="running")
    result.pop("error", None)
    path = directory / "measurement.json"
    write_json(path, result)
    try:
        workload = reference["workload"]
        prefix = ["taskset", "-c", ",".join(map(str, settings["cpus"]))]
        if reuse_capture is not None:
            capture, recording = validate_capture(directory, result, dict(settings=settings,
                architectural_signature=reference["manifest"]["architectural_signature"]))
            print(f"Reusing validated pilot capture: {directory / 'capture'}", flush=True)
        else:
            env = capture_setup(directory / "capture", workload, reference["source"])
            command = [*prefix, "bash", str(runner.PROVIDER), "run"]
            result["capture_command"] = command
            start, end = runner.execute(command, env, directory / "capture/logs/simulator.log",
                                        settings["max_host_seconds"])
            events = read_events(directory / "capture/timing")
            result["capture"] = summarize_capture(events, start, end, settings["interval_ps"])
            result["effective_capture_configuration"] = runner.effective_configuration(
                directory / "capture", workload["dramsys_path"], True)
            result["validation"] = runner.parse_timing((directory / "capture/logs/simulator.log").read_text(),
                workload["cases"][runner.CASE]["manifest"], runner.load_sdk_report(workload["source"]), 16, 1)
            signature = reference["manifest"]["architectural_signature"]
            require(result["validation"]["status"] == "PASS"
                    and all(result["validation"][k] == signature[k] for k in ("cycles", "stage_cycles"))
                    and result["capture"]["roi_simulated_ps"] == signature["roi_simulated_ps"],
                    "capture numerical result or architectural cycle signature changed")
            capture = result["capture"]
            record(directory / "capture", directory / "recording", capture["roi_start_ps"], capture["roi_end_ps"])
            recording, _, _ = load_recording(directory / "recording")
        require(recording["power_interval_ps"] == settings["interval_ps"], "capture interval changed")
        write_json(path, result)
        timing = directory / "replay_timing"
        timing.mkdir()
        replay_env = runner.environment(workload)
        replay_env["CLO_TIMING_DIR"] = str(timing)
        command = [*prefix, *replay_command(directory / "recording", directory / "replay", settings)]
        result["replay_command"] = command
        start, end = runner.execute(command, replay_env, directory / "replay.log", settings["max_host_seconds"])
        summary = json.loads((directory / "replay/summary.json").read_text())
        require(summary["recording_sha256"] == recording["sha256"], "replay did not use its measured capture")
        if "thermal_backend" in settings:
            selected = settings["thermal_backend"]
            backend.verify_metadata(summary, selected, reference["input_sha256"][selected["server"]])
        result["replay"] = summarize_replay(read_events(timing), start, end, summary, settings)
        result["combined"] = combine(capture, result["replay"], result["workflow_start_ns"],
                                     recovered_pilot=reuse_capture is not None)
        evidence = [*timing.rglob("*.jsonl"), *(directory / "capture/timing").glob("*.jsonl"),
                    *(directory / "replay").glob("server*.log"),
                    directory / "capture/logs/simulator.log", directory / "capture/traces/power_hook_trace.jsonl",
                    directory / "capture/results/simulator/gvsoc_config.json",
                    directory / "replay/summary.json", directory / "recording/recording.json"]
        if "thermal_backend" in settings:
            evidence.append(directory / "replay/ice.stk")
        evidence += [directory / "recording" / name for name in recording["sha256"]]
        result["artifact_sha256"] = {str(p.relative_to(directory)): digest(p) for p in sorted(evidence)}
        # Source/input drift invalidates this workflow, including for partial
        # reports and resume; do not leave its measurement marked complete.
        verify_identities(reference)
        result.update(status="complete", loadavg_end=os.getloadavg())
    except BaseException as error:
        result.update(status="interrupted" if isinstance(error, (KeyboardInterrupt, InterruptedError)) else "failed",
                      error=f"{type(error).__name__}: {error}")
        raise
    finally:
        write_json(path, result)
    return result


def settings_for(args, reference):
    base = reference["manifest"]["settings"]
    selected = backend.from_settings(base)
    duration = round(args.max_seconds * 1e12)
    require(math.isfinite(args.max_seconds) and duration > 0 and duration % base["interval_ps"] == 0,
            "replay duration must be a positive integer multiple of 20 µs")
    require(set(base["cpus"]) <= os.sched_getaffinity(0), "reference CPUs are unavailable")
    return dict(case=base["case"], layer=base["layer"], seed=base["seed"], cpus=base["cpus"],
                interval_ps=base["interval_ps"], thermal_threads=selected["solver_threads"],
                thermal_backend=copy.deepcopy(selected), native_cells=67139, grid_pitch_um=50,
                capture_profile="constant", replay_profile="temperature_aware", power_accounting_timing=True,
                replay_simulated_ps=duration, replicates=args.replicates,
                capture_count_per_workflow=1, max_host_seconds=args.max_host_seconds,
                output_policy="normal_replay_histories_and_final_state_no_dense_maps")


def verify_identities(reference):
    for path, checksum in reference["input_sha256"].items():
        require(digest(path) == checksum, f"reference input or native artifact changed: {path}")
    for relative, checksum in reference["source_sha256"].items():
        require(digest(ROOT / relative) == checksum, f"experiment source changed: {relative}")


def run(args):
    reference = load_reference(args.reference_study)
    settings = settings_for(args, reference)
    directory = HERE / "results" / args.prefix
    schedule = [dict(id="pilot", block=0, pilot=True)] + [
        dict(id=f"block{i:02d}", block=i, pilot=False) for i in range(1, args.replicates + 1)]
    if args.command == "plan":
        print(json.dumps(dict(directory=str(directory), settings=settings, schedule=schedule,
                              reference=str(reference["directory"]),
                              note="Each workflow measures one fresh B8 capture, then replay; no runs started"), indent=2))
        return
    directory.parent.mkdir(parents=True, exist_ok=True)
    # Share the architecture study's lock to prevent concurrent timing jobs.
    with (directory.parent / ".runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = dict(directory=str(reference["directory"]), run=str(reference["source"]),
                        input_sha256=reference["input_sha256"])
        if args.resume:
            manifest = json.loads((directory / "study.json").read_text())
            require(manifest["settings"] == settings and manifest["reference"] == identity
                    and manifest["source_sha256"] == reference["source_sha256"]
                    and manifest["schedule"] == schedule, "resume requires identical inputs, settings, and sources")
        else:
            directory.mkdir(exist_ok=False)
            manifest = dict(experiment="capture_replay_overhead_v1", schema_version=1, status="prepared",
                created_utc=datetime.now(timezone.utc).isoformat(), settings=settings, reference=identity,
                architectural_signature=reference["manifest"]["architectural_signature"],
                source_sha256=reference["source_sha256"], schedule=schedule,
                host=runner.host_info(settings["thermal_backend"]))
            for relative in reference["source_sha256"]:
                destination = directory / "source_snapshot" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, destination)
            write_json(directory / "study.json", manifest)
        manifest["status"] = "running"
        write_json(directory / "study.json", manifest)
        try:
            for item in schedule:
                folder = directory / "runs" / item["id"]
                measurement = folder / "measurement.json"
                if measurement.exists() and json.loads(measurement.read_text())["status"] == "complete":
                    from .replay_analysis import validate_measurement
                    validate_measurement(folder, json.loads(measurement.read_text()), manifest)
                    continue
                verify_identities(reference)
                if measurement.exists():
                    saved = json.loads(measurement.read_text())
                    if saved["status"] == "capture_ready":
                        require(item["pilot"], "only a pilot can resume from a recovered capture")
                        workflow(folder, item, reference, settings, reuse_capture=saved)
                        continue
                if folder.exists():
                    folder.rename(folder.with_name(folder.name + f"_failed_{time.time_ns()}"))
                workflow(folder, item, reference, settings)
            manifest["status"] = "complete"
        except BaseException as error:
            manifest["status"] = "interrupted" if isinstance(error, (KeyboardInterrupt, InterruptedError)) else "failed"
            raise
        finally:
            write_json(directory / "study.json", manifest)
        analyze(directory)
        print(f"Completed capture/replay study: {directory}", flush=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("analyze", help="Validate raw evidence and summarize completed workflows")
    report.add_argument("study", type=Path)
    recovery = commands.add_parser("recover-pilot", help="Preserve failed startup and prepare validated pilot capture for resume; runs no simulation")
    recovery.add_argument("study", type=Path)
    smoke = commands.add_parser("smoke", help="Short real capture and 1 ms replay; no B8 benchmark")
    smoke.add_argument("--output", type=Path, required=True)
    smoke.add_argument("--reference-study", type=Path, default=DEFAULT_REFERENCE)
    for name in ("plan", "run"):
        p = commands.add_parser(name)
        p.add_argument("--reference-study", type=Path, default=DEFAULT_REFERENCE)
        p.add_argument("--prefix", default=DEFAULT_PREFIX)
        p.add_argument("--max-seconds", type=runner.positive_float, default=1.)
        p.add_argument("--replicates", type=runner.positive_int, default=1)
        p.add_argument("--max-host-seconds", type=runner.positive_float, help="Optional timeout per capture/replay process")
        p.add_argument("--resume", action="store_true", help="Skip complete workflows; preserve and restart failed workflows")
    args = parser.parse_args(argv)
    if args.command in ("plan", "run") and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.prefix):
        parser.error("prefix must be a simple run name")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.command == "smoke":
            from .replay_smoke import run as smoke_run
            with (HERE / "results/.runner.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                smoke_run(args.output, args.reference_study)
        elif args.command == "analyze":
            result = analyze(args.study)
            print(json.dumps(result["combined"], indent=2))
        elif args.command == "recover-pilot":
            from .replay_recovery import prepare
            prepare(args.study)
        else:
            run(args)
    except (ValueError, FileNotFoundError, BlockingIOError) as error:
        raise SystemExit(f"capture/replay overhead: {error}") from error


if __name__ == "__main__":
    main()
