#!/usr/bin/env python3
"""Run fresh paired co-simulations with the current shared layout workload."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "experiments/layout_opt"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments/llm_dram3d"))
import prepare_workload as prep
import run_study as base
from experiments.layout_opt.transforms import local, write_json
from experiments.layout_opt.config import CASE, OPT_STEP, HEAT_TRANSFER_COEFFICIENT, CAMPAIGN
from experiments.llm_dram3d.opt_steps import opt_metadata, opt_label

DEFAULTS = base.parse_args(["--cases", CASE, "--opt-step", str(OPT_STEP),
                           "--heat-transfer-coefficient", str(HEAT_TRANSFER_COEFFICIENT)])
PROVIDER = ROOT / "Interface_scripts/providers/softhier/provider.sh"


def environment(workload):
    return dict(os.environ, ROOT_DIR=str(ROOT), PYTHON=sys.executable,
        SIMULATOR_CONFIG=workload["arch"], SIMULATOR_APP=workload["source"],
        SOFTHIER_FLOORPLAN="square_bands", SOFTHIER_DRAMSYS_PATH=workload["dramsys_path"],
        SOFTHIER_PRELOAD_MODE="direct", SOFTHIER_INPUT_PRELOAD="",
        SOFTHIER_THERMAL_CONFIG=workload["thermal_config"],
        SOFTHIER_DRAM_POWER_CONFIG=workload["dram_power_config"],
        SOFTHIER_PROVIDER_SCRIPT_DIR=str(PROVIDER.parent),
        OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", PYTHONDONTWRITEBYTECODE="1")


def check_workload(workload):
    expected = dict(layer=DEFAULTS.layer, seed=DEFAULTS.seed, repeats=DEFAULTS.repeats,
        opt_step=OPT_STEP, all_experts=False, estimate_scale=DEFAULTS.estimate_scale,
        plan_only=False, sdk_commit=prep.SDK_COMMIT)
    if any(workload.get(k) != v for k, v in expected.items()) or set(workload["cases"]) != {CASE}:
        raise ValueError("shared workload differs from the fixed experiment settings")
    if workload["native_source_sha256"] != prep.native_source_hashes():
        raise ValueError("native/provider sources changed after workload preparation")
    if workload["source_sha256"] != prep.source_hashes(local(workload["source"])):
        raise ValueError("staged application sources changed")
    thermal = workload["resolved_thermal_config"]
    if thermal["stack_order"] != DEFAULTS.stack_order or thermal["heat_transfer_coefficient_w_m2_k"] != HEAT_TRANSFER_COEFFICIENT:
        raise ValueError("wrong stack order or cooling coefficient")
    for field in ("arch", "thermal_config", "dram_power_config"):
        if prep.sha256(workload[field]) != workload[field + "_sha256"]:
            raise ValueError(f"shared {field} changed")


def prepare(campaign, build_hardware=False):
    folder = local(HERE / "artifacts" / campaign)
    logs = folder / "preparation"
    logs.mkdir(parents=True, exist_ok=True)
    output = folder / "workload"
    manifest = output / "workload.json"
    if manifest.is_file():
        workload = json.loads(manifest.read_text())
        check_workload(workload)
    else:
        print(f"Preparing shared {CASE}, {opt_label(OPT_STEP)}; log: {logs / 'prepare.log'}", flush=True)
        with (logs / "prepare.log").open("w") as log:
            workload = prep.prepare(DEFAULTS.sdk, output, [CASE],
                DEFAULTS.thermal_config, DEFAULTS.dram_power_config,
                layer=DEFAULTS.layer, seed=DEFAULTS.seed,
                estimate_scale=DEFAULTS.estimate_scale, all_experts=DEFAULTS.all_experts,
                repeats=DEFAULTS.repeats, opt_step=OPT_STEP, log=log,
                stack_order=DEFAULTS.stack_order, heat_transfer_coefficient=HEAT_TRANSFER_COEFFICIENT)
    env = environment(workload)
    if build_hardware:
        base.checked(["bash", PROVIDER, "build-hardware"], env, logs / "hardware_build.log")
    case = workload["cases"][CASE]
    if "binary" not in case:
        print(f"Compiling shared application; log: {logs / 'software_build.log'}", flush=True)
        with (logs / "software_build.log").open("w") as log:
            workload = prep.build(workload, log=log)
    for field in ("binary", "preload"):
        case = workload["cases"][CASE]
        if prep.sha256(case[field]) != case[field + "_sha256"]:
            raise ValueError(f"shared {field} changed")
    return folder, workload


def run(campaign, name, layout, build_hardware=False, port=54322):
    folder, workload = prepare(campaign, build_hardware)
    trial = folder / "trials" / name
    layout = local(layout)
    if not layout.is_file():
        raise ValueError("layout JSON is required")
    manifest_path = trial / "study.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        if manifest["layout_sha256"] != prep.sha256(layout):
            raise ValueError("cannot resume with a different layout")
        for filename, expected in manifest["layout_source_sha256"].items():
            if prep.sha256(HERE / filename) != expected:
                raise ValueError("layout implementation changed; use a fresh trial name")
    else:
        trial.mkdir(parents=True, exist_ok=False)
        shutil.copy2(layout, trial / "layout.json")
        shutil.copy2(HERE / "provider.sh", trial / "provider.sh")
        implementation = trial / "implementation"
        implementation.mkdir()
        hashes = {}
        for filename in ("transforms.py", "native_export.py", "provider.sh", "run.py", "config.py"):
            shutil.copy2(HERE / filename, implementation / filename)
            hashes[filename] = prep.sha256(HERE / filename)
        manifest = dict(experiment="llm_dram3d", version=1,
            optimization_experiment="layout_opt", prefix=f"{campaign}_{name}",
            llm_model=prep.MODEL, cases=[CASE], layer=DEFAULTS.layer, seed=DEFAULTS.seed,
            repeats=DEFAULTS.repeats, all_experts=False, **opt_metadata(OPT_STEP),
            interval_ps=DEFAULTS.interval_ps, thermal_step_ps=DEFAULTS.interval_ps,
            target_cells=DEFAULTS.cells, estimate_scale=DEFAULTS.estimate_scale,
            floorplan_rule="square_bands", thermal_replay=None, thermal_replay_till=None,
            allow_model_clamp=False, dram_power_temperature_dependence=False,
            provider_sha256=prep.sha256(trial / "provider.sh"),
            thermal_server_sha256=prep.sha256(ROOT / "3D-ICE/bin/3D-ICE-Server"),
            workload=str((folder / "workload/workload.json").relative_to(ROOT)),
            layout_sha256=prep.sha256(layout), layout=json.loads(layout.read_text()),
            layout_source_sha256=hashes, runs=[])
        for key in ("sdk_commit", "arch_sha256", "source_sha256", "native_source_sha256",
                    "thermal_config_sha256", "dram_power_config_sha256",
                    "resolved_thermal_config", "resolved_dram_power_config"):
            manifest[key] = workload[key]
        write_json(manifest_path, manifest)
    case = workload["cases"][CASE]
    report = prep.load_sdk_report(workload["source"])
    env = environment(workload)
    env.update(SIMULATOR_PROVIDER=str(trial / "provider.sh"),
        LAYOUT_OPT_CONFIG=str(trial / "layout.json"),
        SIMULATOR_PLATFORM=case["preload"], SOFTHIER_BINARY=case["binary"])
    completed = {entry["profile"] for entry in manifest["runs"]}
    for profile in base.PROFILES:
        if prep.sha256(ROOT / "3D-ICE/bin/3D-ICE-Server") != manifest["thermal_server_sha256"]:
            raise ValueError("native thermal solver changed since trial preparation")
        if profile in completed:
            continue
        run_dir = trial / "runs" / profile
        if run_dir.exists():
            raise ValueError(f"incomplete run preserved at {run_dir}; use a new trial name")
        # Existing probes record ROI timing; this never captures/repeats activity.
        (run_dir / "timing").mkdir(parents=True)
        env.update(SOFTHIER_RUN_CWD=str(run_dir / "results/simulator"),
                   CLO_TIMING_DIR=str(run_dir / "timing"))
        started = time.monotonic()
        base.checked(["make", "coupled-run", "RUN_NAME=layout_opt", f"RUN_DIR={run_dir}",
            "BUILD_SIMULATOR=0", "BUILD_3DICE=0", f"PORT={port}", f"SOFTHIER_POWER_PROFILE={profile}",
            f"POWER_INTERVAL_PS={DEFAULTS.interval_ps}",
            f"ICE_SLOT_SECONDS={DEFAULTS.interval_ps * 1e-12:.12g}",
            f"ICE_STEP_SECONDS={DEFAULTS.interval_ps * 1e-12:.12g}",
            f"ICE_TARGET_TOP_DIE_CELLS={DEFAULTS.cells}"], env, trial / f"{profile}.log")
        contract = json.loads((run_dir / "generated/system_config.json").read_text())
        if contract["metadata"]["layout_optimization"]["layout_sha256"] != manifest["layout_sha256"]:
            raise ValueError("exported contract did not use the archived layout")
        timing = base.parse_timing((run_dir / "logs/simulator.log").read_text(), case["manifest"],
            report, contract["metadata"]["cluster_grid"]["count"], DEFAULTS.repeats,
            require_validation=False, opt_step=OPT_STEP)
        timing["host_seconds"] = time.monotonic() - started
        write_json(run_dir / "results/simulator/validation.json", timing)
        for field in ("binary", "preload"):
            if prep.sha256(case[field]) != case[field + "_sha256"]:
                raise ValueError(f"{field} changed during optimization")
        base.archive(workload, case, run_dir / "artifacts", trial / "provider.sh")
        if prep.sha256(ROOT / "3D-ICE/bin/3D-ICE-Server") != manifest["thermal_server_sha256"]:
            raise ValueError("native thermal solver changed during the run")
        shutil.copy2(trial / "layout.json", run_dir / "artifacts/layout.json")
        shutil.copytree(trial / "implementation", run_dir / "artifacts/layout_implementation")
        manifest["runs"].append(dict(case=CASE, profile=profile, interval_ps=DEFAULTS.interval_ps,
            thermal_step_ps=DEFAULTS.interval_ps, repeats=DEFAULTS.repeats, **opt_metadata(OPT_STEP),
            path=str(run_dir.relative_to(ROOT)), binary_sha256=case["binary_sha256"],
            preload_sha256=case["preload_sha256"], numerical_validation=timing))
        write_json(manifest_path, manifest)
        print(f"Completed {name}/{profile}: {timing['layer_time_us']:.3f} us", flush=True)
    print(f"Completed trial: {manifest_path}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    parser.add_argument("--campaign", default=CAMPAIGN)
    parser.add_argument("--name")
    parser.add_argument("--layout", type=Path)
    parser.add_argument("--build-hardware", action="store_true")
    parser.add_argument("--port", type=int, default=54322, help="private solver port for this trial")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    for value in (args.campaign, args.name):
        if value is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
            parser.error("campaign and name must be simple directory names")
    if args.action == "run":
        if args.name is None or args.layout is None:
            parser.error("run requires --name and --layout")
        run(args.campaign, args.name, args.layout, args.build_hardware, args.port)
    else:
        prepare(args.campaign, args.build_hardware)


if __name__ == "__main__":
    main()
