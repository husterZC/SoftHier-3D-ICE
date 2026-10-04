#!/usr/bin/env python3
"""Run paired leakage-profile studies of the SDK's complete LLM decoder layer."""

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

from prepare_workload import MODELS, ROOT, SDK, build, prepare, sha256, validate_settings, write_json

sys.path.insert(0, str(ROOT))
from Interface_scripts.providers.softhier.floorplans import DEFAULT_RULE, RULES

STUDY = ROOT / "experiments/llm_decode"
PROVIDER = ROOT / "Interface_scripts/providers/softhier/provider.sh"
PROFILES = ("constant", "temperature_aware")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "Runs both leakage profiles for every batch. Use analyze.py --animations "
        "to generate thermal GIFs. Existing studies are not overwritten."))
    parser.add_argument("--llm_model", "--llm-model", choices=MODELS, default="gpt-oss-120b")
    parser.add_argument("--decode_batch", "--decode-batch", nargs="+", type=int, default=[1],
        help="one or more decode batch sizes, e.g. 1 8 (default: 1)")
    parser.add_argument("--repeats", type=int, default=1,
        help="complete layer repetitions with the same input/cache position (default: 1)")
    parser.add_argument("--interval-ps", type=int, default=20_000_000,
        help="power/temperature exchange interval in ps; default 20000000 = 20 us")
    parser.add_argument("--cells", type=int, default=16384,
        help="approximate top-die thermal cell target (default: %(default)s)")
    parser.add_argument("--floorplan", choices=tuple(RULES), default=DEFAULT_RULE)
    parser.add_argument("--estimate-scale", type=float, default=1.0,
        help="new-logic power coefficient multiplier (default: %(default)s)")
    parser.add_argument("--prefix", default="decode",
        help="new study name for logs, workloads and raw runs; no path separators")
    parser.add_argument("--build-hardware", action="store_true",
        help="build native simulator models once; requires make bootstrap first")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.prefix):
        parser.error("prefix must be a simple run name without path separators")
    if min(args.interval_ps, args.cells, args.repeats) <= 0:
        parser.error("interval, cell count, and repeats must be positive")
    if not math.isfinite(args.estimate_scale) or args.estimate_scale <= 0:
        parser.error("estimate scale must be finite and positive")
    if not args.decode_batch or len(set(args.decode_batch)) != len(args.decode_batch) or min(args.decode_batch) <= 0:
        parser.error("decode batches must be positive and must not be repeated")
    return args


def checked(command, env, log):
    print(f"Running {' '.join(map(str, command))}\n  log: {log}", flush=True)
    with log.open("w") as stream:
        subprocess.run(list(map(str, command)), cwd=ROOT, env=env,
            stdout=stream, stderr=subprocess.STDOUT, check=True)


def parse_timing(text, batch, model, clusters, repeats=1):
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    if "DECODE_DONE" not in text or "DECODE_FAIL" in text:
        raise ValueError("decoder did not complete successfully")
    def one(pattern, label):
        matches = re.findall(pattern, text, re.M)
        if len(matches) != 1:
            raise ValueError(f"expected exactly one {label} record")
        return matches[0]
    cycles = int(one(r"^LAYER_CYCLES (\d+)$", "layer timing"))
    period = int(one(r"Execution period is (\d+) ns", "execution period"))
    stages = re.findall(r"^STAGE (\w+) (\d+)$", text, re.M)
    if len(stages) != 14 or len(dict(stages)) != 14 or sum(int(v) for _, v in stages) != cycles:
        raise ValueError("decoder stage counters do not sum to layer cycles")
    if cycles <= 0 or not 0 <= period - cycles < 10000:
        raise ValueError("layer clock mismatch or wrapped cycle counter")
    experts, assignments = map(int, one(r"ROUTING distinct_experts (\d+) assignments (\d+)", "routing"))
    if assignments != batch * model["experts_per_token"] or not 0 < experts <= min(assignments, model["num_experts"]):
        raise ValueError("decoder routing count disagrees with the requested batch")
    preload_cycles = re.findall(r"Direct ELF preload complete .*cycle: (\d+)\)", text)
    if len(preload_cycles) != clusters + 1 or set(preload_cycles) != {"0"}:
        raise ValueError("direct preload did not complete at cycle zero for every loader")
    return {"decode_batch": batch, "repeats": repeats, "layer_cycles": cycles,
        "layer_ms": cycles * 1e-6, "execution_period_ns": period,
        "stages": {name: int(value) for name, value in stages},
        "distinct_experts": experts, "assignments": assignments,
        "preload_cycles": 0, "loader_count": len(preload_cycles)}


def archive(workload, output, destination, batch, provider):
    destination.mkdir()
    sources = {"softhier.elf": Path(workload["binary"]),
        "preload.elf": Path(workload["weights"]),
        "input.elf": Path(workload["inputs"][str(batch)]["path"]),
        "provider.sh": provider}
    sources.update({name: output / name for name in
        ("workload.json", "manifest.json", "arch.py", "model.json", "main.c")})
    for name, source in sources.items():
        # Independent copies, using copy-on-write where the filesystem supports
        # it. Large immutable weights need not consume 7 GB per profile.
        subprocess.run(["cp", "--reflink=auto", str(source), str(destination / name)], check=True)
    shutil.copytree(output / "environment", destination / "environment")


def validate_numerics(workload, output, run_dir, batch, env, log):
    # Reuse the SDK's independent NumPy reference against this run's dump.
    reference = run_dir / "results/reference"
    reference.mkdir()
    for name in ("manifest.json", "weights.elf", f"input-b{batch}.elf"):
        (reference / name).symlink_to(output / name)
    (reference / f"batch-{batch}").symlink_to(run_dir / "results/simulator")
    checked([sys.executable, Path(workload["source"]) / "tools/reference.py",
        "--build-dir", reference, "--batches", batch], env, log)
    return json.loads((run_dir / "results/simulator/validation.json").read_text())


def main(argv=None):
    args = parse_args(argv)
    model = json.loads((SDK / "LLM_decode" / args.llm_model / "config/model.json").read_text())
    validate_settings(model, args.decode_batch, args.repeats, args.estimate_scale)
    logs, output = STUDY / "logs" / args.prefix, STUDY / "workloads" / args.prefix
    run_dirs = {(batch, profile): ROOT / "runs/llm_decode" /
        f"{args.prefix}_{args.llm_model}_b{batch}_{profile}"
        for batch in args.decode_batch for profile in PROFILES}
    for path in (logs, output, *run_dirs.values()):
        if path.exists():
            raise ValueError(f"refusing to overwrite existing study artifacts: {path}")
    logs.mkdir(parents=True)
    frozen_provider = logs / "provider.sh"
    shutil.copy2(PROVIDER, frozen_provider)
    manifest = {"experiment": "llm_decode", "version": 1, "prefix": args.prefix,
        "llm_model": args.llm_model, "decode_batches": args.decode_batch,
        "repeats": args.repeats, "interval_ps": args.interval_ps,
        "target_cells": args.cells, "estimate_scale": args.estimate_scale,
        "floorplan_rule": args.floorplan, "provider_sha256": sha256(frozen_provider),
        "runs": []}
    manifest_path = logs / "study.json"
    write_json(manifest_path, manifest)
    print(f"Generating shared weights and inputs; log: {logs / 'prepare.log'}", flush=True)
    with (logs / "prepare.log").open("w") as log:
        workload = prepare(SDK, args.llm_model, output, args.decode_batch,
            args.repeats, args.estimate_scale, log=log)
    env = os.environ.copy()
    env.update(PYTHON=sys.executable, SIMULATOR_CONFIG=workload["arch"],
        SIMULATOR_APP=workload["source"], SIMULATOR_PLATFORM=workload["weights"],
        SOFTHIER_FLOORPLAN=args.floorplan, SOFTHIER_DRAMSYS_PATH=workload["dramsys_path"],
        SOFTHIER_PRELOAD_MODE="direct", SIMULATOR_PROVIDER=str(frozen_provider),
        SOFTHIER_PROVIDER_SCRIPT_DIR=str(PROVIDER.parent),
        OPENBLAS_NUM_THREADS="1")
    if args.build_hardware:
        checked(["bash", frozen_provider, "build-hardware"], env, logs / "hardware_build.log")
    print(f"Building the SDK decoder; log: {logs / 'software_build.log'}", flush=True)
    with (logs / "software_build.log").open("w") as log:
        workload = build(workload, output, log=log)
    env["SOFTHIER_BINARY"] = workload["binary"]
    manifest.update(sdk_commit=workload["sdk_commit"],
        binary_sha256=workload["binary_sha256"], weights_sha256=workload["weights_sha256"],
        workload=str((output / "workload.json").relative_to(ROOT)))
    write_json(manifest_path, manifest)
    for (batch, profile), run_dir in run_dirs.items():
        env.update(SOFTHIER_INPUT_PRELOAD=workload["inputs"][str(batch)]["path"],
            SOFTHIER_RUN_CWD=str(run_dir / "results/simulator"))
        started = time.monotonic()
        checked(["make", "coupled-run", "RUN_NAME=llm_decode", f"RUN_DIR={run_dir}",
            "BUILD_SIMULATOR=0", "BUILD_3DICE=0", f"SOFTHIER_POWER_PROFILE={profile}",
            f"POWER_INTERVAL_PS={args.interval_ps}", f"ICE_STEP_SECONDS={args.interval_ps * 1e-13:.12g}",
            f"ICE_TARGET_TOP_DIE_CELLS={args.cells}"], env, logs / f"b{batch}_{profile}.log")
        contract = json.loads((run_dir / "generated/system_config.json").read_text())
        timing = parse_timing((run_dir / "logs/simulator.log").read_text(), batch, model,
            contract["metadata"]["cluster_grid"]["count"], args.repeats)
        timing["host_seconds"] = time.monotonic() - started
        write_json(run_dir / "results/simulator/timing.json", timing)
        validation = validate_numerics(workload, output, run_dir, batch, env,
            logs / f"b{batch}_{profile}_reference.log")
        if sha256(Path(workload["binary"])) != workload["binary_sha256"]:
            raise ValueError("decoder binary changed during the paired study")
        archive(workload, output, run_dir / "artifacts", batch, frozen_provider)
        manifest["runs"].append({"llm_model": args.llm_model, "decode_batch": batch,
            "profile": profile, "interval_ps": args.interval_ps, "repeats": args.repeats,
            "path": str(run_dir.relative_to(ROOT)), "binary_sha256": workload["binary_sha256"],
            "preload_sha256": workload["weights_sha256"],
            "input_sha256": workload["inputs"][str(batch)]["sha256"],
            "timing": timing, "numerical_validation": validation})
        write_json(manifest_path, manifest)
        print(f"Completed batch {batch}, {profile}: layer {timing['layer_ms']:.6f} ms", flush=True)
    print(f"Completed study manifest: {manifest_path}", flush=True)


if __name__ == "__main__":
    main()
