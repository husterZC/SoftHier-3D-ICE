#!/usr/bin/env python3
"""Run paired logic-leakage profiles for the SDK memory-on-logic layer cases."""

import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from prepare_workload import (CASES, MODEL, ROOT, SDK, build, load_sdk_report,
    native_source_hashes, prepare, sha256, validate_inputs, validate_repeats,
    validate_settings, within_repo, write_json)
from Interface_scripts.providers.softhier.floorplans import RULES

STUDY = ROOT / "experiments/llm_dram3d"
PROVIDER = ROOT / "Interface_scripts/providers/softhier/provider.sh"
CONFIGS = PROVIDER.parent / "configs"
PROFILES = ("constant", "temperature_aware")


def replay_seconds(value):
    try:
        seconds = float(str(value).removesuffix("s"))
        if math.isfinite(seconds) and seconds > 0 and round(seconds * 1e12) >= 1:
            return seconds
    except (ValueError, OverflowError):
        pass
    raise argparse.ArgumentTypeError("replay duration must be positive seconds, e.g. 0.1s, 1s or 2s")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, default=SDK)
    parser.add_argument("--cases", choices=CASES, nargs="+", default=[CASES[0]])
    parser.add_argument("--thermal-config", type=Path, default=CONFIGS / "dram3d_thermal.json")
    parser.add_argument("--dram-power-config", type=Path, default=CONFIGS / "dram3d_power.json")
    parser.add_argument("--layer", type=int, default=1)
    parser.add_argument("--seed", type=int, default=19)
    parser.add_argument("--opt-step", type=int, choices=range(27), default=0, metavar="0..26",
                        help="SDK decode optimization step (default: 0)")
    parser.add_argument("--repeats", type=int, default=1,
                        help="repeat the complete layer with the same inputs and KV-cache position")
    replay = parser.add_mutually_exclusive_group()
    replay.add_argument("--thermal-replay", type=int, metavar="BLOCKS",
                        help="thermally replay a block of --repeats layers BLOCKS times per profile")
    replay.add_argument("--thermal-replay-till", type=replay_seconds, nargs="?", const=1., metavar="SECONDS",
                        help="replay until this simulated time per profile (default when supplied: 1s)")
    parser.add_argument("--allow-model-clamp", action="store_true",
                        help="allow thermal replay above 125 C using GVSoC's endpoint leakage clamp")
    parser.add_argument("--all-experts", action="store_true")
    parser.add_argument("--interval-ps", type=int, default=20_000_000,
                        help="power/temperature interval and 3D-ICE timestep in picoseconds")
    parser.add_argument("--cells", type=int, default=16384)
    parser.add_argument("--floorplan", choices=tuple(RULES), default="square_bands",
                        help="logic placement; DRAM channel regions stay aligned with their clusters")
    parser.add_argument("--estimate-scale", type=float, default=1.0)
    parser.add_argument("--prefix", default="dram3d")
    parser.add_argument("--build-hardware", action="store_true")
    args = parser.parse_args(argv)
    if args.allow_model_clamp and args.thermal_replay is None and args.thermal_replay_till is None:
        parser.error("--allow-model-clamp requires thermal replay")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.prefix):
        parser.error("prefix must be a simple run name without path separators")
    if min(args.interval_ps, args.cells) <= 0:
        parser.error("interval and cell count must be positive")
    try:
        validate_settings(args.cases, args.layer, args.seed, args.estimate_scale, args.repeats, args.opt_step)
        if args.thermal_replay is not None:
            validate_repeats(args.thermal_replay)
    except ValueError as error:
        parser.error(str(error))
    return args


def checked(command, env, log):
    print(f"Running {' '.join(map(str, command))}\n  log: {log}", flush=True)
    with log.open("w") as stream:
        subprocess.run(list(map(str, command)), cwd=ROOT, env=env,
            stdout=stream, stderr=subprocess.STDOUT, check=True)


def parse_timing(text, manifest, report, clusters, repeats=1, *, require_validation=True, opt_step=None):
    validate_repeats(repeats)
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    if len(re.findall(r"LLM_DRAM3D_(?:PASS|DONE)\b", text)) != 1 or "LLM_DRAM3D_FAIL" in text:
        raise ValueError("expected exactly one successful SDK layer completion report")
    markers = re.findall(r"^LLM_DRAM3D_REPEATS count=(\d+)$", text, re.M)
    if (repeats == 1 and "LLM_DRAM3D_REPEATS" in text) or (repeats > 1 and (
            markers != [str(repeats)] or text.count("LLM_DRAM3D_REPEATS") != 1)):
        raise ValueError("SDK completed repeat count differs from the requested repeats")
    if repeats > 1:
        pattern = r"^LLM_STAGE id=(\d+) cycles_hi=(\d+) cycles_lo=(\d+)$"
        stages = re.findall(pattern, text, re.M)
        if len(stages) != 11 or text.count("LLM_STAGE") != 11:
            raise ValueError("expected 11 accumulated SDK stage counters")
        if any(int(word) > 0xffffffff for _, hi, lo in stages for word in (hi, lo)):
            raise ValueError("invalid 32-bit word in accumulated SDK stage counter")
        text = re.sub(pattern, lambda m:
            f"LLM_STAGE id={m[1]} cycles={(int(m[2]) << 32) | int(m[3])}", text, flags=re.M)
    result = report(text, manifest)
    if opt_step is not None and result.get("optimization_step", 0) != opt_step:
        raise ValueError("SDK optimization step differs from the requested step")
    if require_validation and result["status"] != "PASS":
        raise ValueError("SDK numerical validation is required but was skipped")
    preload = re.findall(r"Direct ELF preload complete .*cycle: (\d+)\)", text)
    if len(preload) != clusters + 1 or set(preload) != {"0"}:
        raise ValueError("direct preload did not complete at cycle zero for every loader")
    return dict(result, repeats=repeats, mean_layer_time_us=result["layer_time_us"] / repeats,
                preload_cycles=0, loader_count=len(preload))


def archive(workload, case, destination, provider):
    if workload["native_source_sha256"] != native_source_hashes():
        raise ValueError("native/provider sources changed since workload preparation; cannot archive this run accurately")
    destination.mkdir()
    sources = {"layer.elf": Path(case["binary"]), "preload.elf": Path(case["preload"]),
        "provider.sh": provider, "manifest.json": Path(case["output"]) / "manifest.json",
        "arch.py": Path(workload["arch"]),
        "workload.json": Path(workload["source"]).parent / "workload.json",
        "thermal_config.json": Path(workload["thermal_config"]),
        "dram_power_config.json": Path(workload["dram_power_config"])}
    for name, source in sources.items():
        # Independent artifacts; sparse/copy-on-write preload copies where supported.
        subprocess.run(["cp", "--reflink=auto", "--sparse=always", str(source),
                        str(destination / name)], check=True)
    shutil.copytree(workload["source"], destination / "source",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(Path(workload["source"]).parent / "native_source", destination / "native_source")
    shutil.copytree(workload["dramsys_path"], destination / "environment")


def replay_bounds(source):
    """Read absolute MMIO timer bounds from the existing native timing probes."""
    events = []
    for path in sorted((source / "timing").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            event = json.loads(line)
            if event.get("actor") == "gvsoc" and event.get("phase") in ("roi_start", "roi_end"):
                events.append(event)
    starts = [event for event in events if event["phase"] == "roi_start"]
    ends = [event for event in events if event["phase"] == "roi_end"]
    if len(starts) != 1 or len(ends) != 1:
        raise ValueError("thermal replay requires one native timer start/end pair; rebuild hardware with timing probes")
    start, end = starts[0]["sim_ps"], ends[0]["sim_ps"]
    if (type(start) is not int or type(end) is not int or not 0 <= start < end
            or starts[0]["pid"] != ends[0]["pid"]):
        raise ValueError("invalid native replay timer bounds")
    periods = re.findall(r"\[Performance Counter\]: Execution period is (\d+) ns",
                         (source / "logs/simulator.log").read_text())
    if periods != [str((end - start) // 1000)]:
        raise ValueError("native timer bounds differ from the simulator performance counter")
    return start, end


def run_replays(source_entry, recording, destinations, cycles, logs, env, seconds=None, allow_model_clamp=False):
    from experiments.thermal_replay.record import record

    source = within_repo(ROOT / source_entry["path"])
    start, end = replay_bounds(source)
    repeats, name = source_entry["repeats"], source_entry["case"]
    manifest = record(source, recording, start=start, end=end, layers_per_cycle=repeats)
    slot = manifest["power_interval_ps"]
    # Extend the engine's default one-second cap through the final thermal slot.
    stop = round(seconds * 1e12) if seconds is not None else start + cycles * (end - start)
    stop = ((stop + slot - 1) // slot) * slot
    cap = seconds if seconds is not None else stop / 1e12
    entries = []
    for profile in PROFILES:
        destination = within_repo(destinations[profile])
        checked([sys.executable, "-m", "experiments.thermal_replay", "replay",
            "--recording", recording, "--output", destination, "--profile", profile,
            "--max-seconds", cap, "--thermal-step", source_entry["thermal_step_ps"] / 1e12]
            + (["--cycles", cycles] if cycles is not None else [])
            + (["--allow-model-clamp"] if allow_model_clamp else []),
            env, logs / f"{name}_{profile}_replay.log")
        summary = json.loads((destination / "summary.json").read_text())
        expected = cycles if cycles is not None else max(0, (stop - start) // (end - start))
        if (summary.get("status") != ("time_limit" if seconds is not None else "complete")
                or summary.get("allow_model_clamp", False) != allow_model_clamp
                or summary.get("cycles_completed") != expected
                or summary.get("layers_completed") != expected * repeats
                or (seconds is not None and (summary.get("slots") != stop // slot
                    or round(summary.get("simulated_seconds", 0) * 1e12) != stop))):
            raise ValueError(f"thermal replay did not reach the requested count/duration: {summary.get('status')}")
        entries.append(dict(case=name, profile=profile, repeats=repeats,
            opt_step=source_entry.get("opt_step", 0), allow_model_clamp=allow_model_clamp,
            interval_ps=slot, thermal_step_ps=source_entry["thermal_step_ps"],
            path=str(destination.relative_to(ROOT)), source_run=source_entry["path"],
            recording=str(recording.relative_to(ROOT)), replay_cycles=expected, replay_seconds=seconds))
    return entries


def main(argv=None):
    args = parse_args(argv)
    replay = args.thermal_replay is not None or args.thermal_replay_till is not None
    # Fail on incomplete physical/electrical input before building or preloading.
    validate_inputs(args.thermal_config, args.dram_power_config)
    logs, output = STUDY / "logs" / args.prefix, STUDY / "workloads" / args.prefix
    source_profiles = ("constant",) if replay else PROFILES
    source_tag = "source_" if replay else ""
    run_dirs = {(case, profile): ROOT / "runs/llm_dram3d" / f"{args.prefix}_{case}_{source_tag}{profile}"
                for case in args.cases for profile in source_profiles}
    replay_dirs = {(case, profile): ROOT / "runs/llm_dram3d" / f"{args.prefix}_{case}_{profile}"
                   for case in args.cases for profile in PROFILES} if replay else {}
    for path in (logs, output, *run_dirs.values(), *replay_dirs.values()):
        if path.exists():
            raise ValueError(f"refusing to overwrite existing study artifacts: {path}")
    logs.mkdir(parents=True)
    frozen_provider = logs / "provider.sh"
    shutil.copy2(PROVIDER, frozen_provider)
    manifest = dict(experiment="llm_dram3d", version=1, prefix=args.prefix,
        llm_model=MODEL, cases=args.cases, layer=args.layer, seed=args.seed, repeats=args.repeats,
        all_experts=args.all_experts, interval_ps=args.interval_ps, thermal_step_ps=args.interval_ps,
        target_cells=args.cells, opt_step=args.opt_step, thermal_replay=args.thermal_replay,
        thermal_replay_till=args.thermal_replay_till, allow_model_clamp=args.allow_model_clamp,
        estimate_scale=args.estimate_scale, floorplan_rule=args.floorplan,
        provider_sha256=sha256(frozen_provider), dram_power_temperature_dependence=False, runs=[])
    manifest_path = logs / "study.json"
    if replay:
        manifest["source_runs"] = []
    write_json(manifest_path, manifest)
    with (logs / "prepare.log").open("w") as log:
        workload = prepare(args.sdk, output, args.cases, args.thermal_config, args.dram_power_config,
            layer=args.layer, seed=args.seed, estimate_scale=args.estimate_scale,
            all_experts=args.all_experts, repeats=args.repeats, opt_step=args.opt_step, log=log)
    env = dict(os.environ, PYTHON=sys.executable, SIMULATOR_CONFIG=workload["arch"],
        SIMULATOR_APP=workload["source"], SOFTHIER_FLOORPLAN=args.floorplan,
        SOFTHIER_DRAMSYS_PATH=workload["dramsys_path"], SOFTHIER_PRELOAD_MODE="direct",
        SOFTHIER_INPUT_PRELOAD="", SOFTHIER_THERMAL_CONFIG=workload["thermal_config"],
        SOFTHIER_DRAM_POWER_CONFIG=workload["dram_power_config"],
        SIMULATOR_PROVIDER=str(frozen_provider), SOFTHIER_PROVIDER_SCRIPT_DIR=str(PROVIDER.parent),
        OPENBLAS_NUM_THREADS="1", PYTHONDONTWRITEBYTECODE="1")
    if args.build_hardware:
        checked(["bash", frozen_provider, "build-hardware"], env, logs / "hardware_build.log")
    with (logs / "software_build.log").open("w") as log:
        workload = build(workload, log=log)
    for key in ("sdk_commit", "arch_sha256", "source_sha256", "native_source_sha256", "thermal_config_sha256",
                "dram_power_config_sha256", "resolved_thermal_config", "resolved_dram_power_config"):
        manifest[key] = workload[key]
    manifest["workload"] = str((output / "workload.json").relative_to(ROOT))
    write_json(manifest_path, manifest)
    report = load_sdk_report(workload["source"])
    if workload["native_source_sha256"] != native_source_hashes():
        raise ValueError("native/provider sources changed during preparation/build; start a fresh study")
    for (name, profile), run_dir in run_dirs.items():
        case = workload["cases"][name]
        env.update(SIMULATOR_PLATFORM=case["preload"], SOFTHIER_BINARY=case["binary"],
                   SOFTHIER_RUN_CWD=str(run_dir / "results/simulator"))
        if replay:
            timing_dir = run_dir / "timing"
            timing_dir.mkdir(parents=True)
            env["CLO_TIMING_DIR"] = str(timing_dir)
        started = time.monotonic()
        checked(["make", "coupled-run", "RUN_NAME=llm_dram3d", f"RUN_DIR={run_dir}",
            "BUILD_SIMULATOR=0", "BUILD_3DICE=0", f"SOFTHIER_POWER_PROFILE={profile}",
            f"POWER_INTERVAL_PS={args.interval_ps}", f"ICE_SLOT_SECONDS={args.interval_ps * 1e-12:.12g}",
            f"ICE_STEP_SECONDS={args.interval_ps * 1e-12:.12g}",
            f"ICE_TARGET_TOP_DIE_CELLS={args.cells}"], env, logs / f"{name}_{profile}.log")
        contract = json.loads((run_dir / "generated/system_config.json").read_text())
        if contract["contract"]["version"] != 2:
            raise ValueError("memory-on-logic experiment requires the multi-die contract")
        timing = parse_timing((run_dir / "logs/simulator.log").read_text(), case["manifest"],
            report, contract["metadata"]["cluster_grid"]["count"], args.repeats,
            require_validation=False, opt_step=args.opt_step)
        timing["host_seconds"] = time.monotonic() - started
        write_json(run_dir / "results/simulator/validation.json", timing)
        for field in ("binary", "preload"):
            if sha256(case[field]) != case[field + "_sha256"]:
                raise ValueError(f"{field} changed during paired study")
        archive(workload, case, run_dir / "artifacts", frozen_provider)
        entry = dict(case=name, profile=profile, interval_ps=args.interval_ps,
            repeats=args.repeats, opt_step=args.opt_step, thermal_step_ps=args.interval_ps,
            path=str(run_dir.relative_to(ROOT)), binary_sha256=case["binary_sha256"],
            preload_sha256=case["preload_sha256"], numerical_validation=timing)
        if replay:
            manifest["source_runs"].append(entry)
            write_json(manifest_path, manifest)
            target = (f"until {args.thermal_replay_till:g}s" if args.thermal_replay_till is not None
                      else f"of {args.thermal_replay} blocks")
            print(f"Captured {name}: {args.repeats} layer(s); starting replay {target} per profile", flush=True)
            replay_env = dict(env)
            replay_env.pop("CLO_TIMING_DIR", None)
            manifest["runs"].extend(run_replays(entry, Path(case["output"]) / "recording",
                {p: replay_dirs[name, p] for p in PROFILES}, args.thermal_replay, logs, replay_env,
                seconds=args.thermal_replay_till, allow_model_clamp=args.allow_model_clamp))
        else:
            manifest["runs"].append(entry)
        write_json(manifest_path, manifest)
        print(f"Completed {name}, {profile}: {args.repeats} repeat(s), "
              f"{timing['layer_time_us']:.3f} us total, "
              f"{timing['mean_layer_time_us']:.3f} us/layer", flush=True)
    print(f"Completed study manifest: {manifest_path}", flush=True)


if __name__ == "__main__":
    main()
