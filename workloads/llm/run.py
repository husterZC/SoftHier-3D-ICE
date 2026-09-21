#!/usr/bin/env python3
"""Stage, build and run one independent LLM phase through the existing provider."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from prepare import ROOT, prepare, sha256


def execute(command, env, log=None):
    print("Running: " + " ".join(map(str, command)), flush=True)
    if log is None:
        subprocess.run(command, env=env, cwd=ROOT, check=True)
    else:
        print(f"Log: {log}", flush=True)
        with log.open("w") as stream:
            result = subprocess.run(command, env=env, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
        if result.returncode:
            print("\n".join(log.read_text(errors="replace").splitlines()[-40:]), file=sys.stderr)
            raise subprocess.CalledProcessError(result.returncode, command)


def positive_int(value):
    value = int(value)
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("prefill", "decode"), default="prefill")
    parser.add_argument("--preset", choices=("smoke", "original"), default="smoke")
    parser.add_argument("--mode", choices=("coupled", "standalone", "build"), default="coupled")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--arch", type=Path)
    parser.add_argument("--power-profile", choices=("constant", "temperature_aware"), default="constant")
    parser.add_argument("--power-interval-ps", type=positive_int)
    parser.add_argument("--thermal-cells", type=positive_int)
    parser.add_argument("--build-hardware", action="store_true", default=os.environ.get("LLM_BUILD_HARDWARE") == "1")
    parser.add_argument("--regression", action="store_true", help="run the on-device kernel regression instead of a layer")
    args = parser.parse_args()
    run = (args.run_dir or ROOT / "runs" / f"llm_{args.phase}_{args.preset}" /
           datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")).resolve()
    stage = run / "generated/llm"
    provider = ROOT / "Interface_scripts/providers/softhier/provider.sh"
    sdk = Path(os.environ.get("SOFTHIER_SDK_DIR",
        Path(os.environ.get("SOFTHIER_DIR", ROOT / "SoftHier")) / "soft_hier_sdk")).resolve()
    try:
        manifest = prepare(sdk, stage, args.phase, args.preset, args.arch, args.regression)
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"LLM preparation failed: {error}\n")
    logs = run / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    build = run / "build/llm"
    interval = args.power_interval_ps or (10000000 if args.preset == "smoke" else 100000000)
    cells = args.thermal_cells or (4096 if args.preset == "smoke" else 65536)
    env = os.environ.copy()
    env.update(SIMULATOR_CONFIG=manifest["architecture"], SIMULATOR_APP=manifest["app"],
        SIMULATOR_PLATFORM="", SOFTHIER_CONFIG_OPTIONS_FILE=manifest["simulator_options_file"], SOFTHIER_SDK_DIR=str(sdk), SOFTHIER_SW_BUILD=str(build), SOFTHIER_RUNTIME_WORKDIR=str(stage / "runtime"),
        SOFTHIER_POWER_PROFILE=args.power_profile, PYTHONDONTWRITEBYTECODE="1")
    manifest.update(mode=args.mode, power_profile=args.power_profile,
        core_model=env.get("SOFTHIER_CORE_MODEL") or "fast",
        floorplan=env.get("SOFTHIER_FLOORPLAN", "redmule_strip"), power_interval_ps=interval,
        thermal_cells=cells, build_directory=str(build), status="building")
    manifest_path = stage / "workload.json"
    def save():
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    save()
    try:
        execute([str(provider), "build" if args.build_hardware else "build-workload"], env, logs / "workload-build.log")
        artifacts = run / "artifacts/llm"
        artifacts.mkdir(parents=True, exist_ok=True)
        softhier = Path(env.get("SOFTHIER_DIR", ROOT / "SoftHier")).resolve()
        workdir = Path(env.get("SOFTHIER_WORKDIR", softhier / ".power_interface")).resolve()
        models = artifacts / "models"
        execute([sys.executable, str(ROOT / "workloads/llm/build_model.py"),
            "--softhier", str(softhier), "--workdir", str(workdir),
            "--stage", str(stage / "native"), "--models", str(models)], env, logs / "model-build.log")
        env["SOFTHIER_MODEL_DIR"] = str(models)
        env["SOFTHIER_ENGINE_DIR"] = str(models / "engine")
        manifest["native_frontend_sha256"] = sha256(models / "engine/gvsoc")
        manifest["native_model_sha256"] = {str(p.relative_to(artifacts)): sha256(p) for p in models.rglob("*.so")}
        manifest["native_source_sha256"] = {str(p.relative_to(stage)): sha256(p)
            for p in (stage / "native").rglob("*") if p.is_file()}
        manifest["simulator_revisions"] = {name: subprocess.check_output(
            ["git", "-C", str(softhier / name), "rev-parse", "HEAD"], text=True).strip()
            for name in (".", "engine", "core", "pulp")}
        shutil.copy2(build / "softhier.elf", artifacts / "softhier.elf")
        manifest["elf"] = str(artifacts / "softhier.elf")
        manifest["elf_sha256"] = sha256(artifacts / "softhier.elf")
        manifest["generated_sha256"] = {str(p.relative_to(stage)): sha256(p) for p in stage.rglob("*")
            if p.is_file() and p.suffix in (".h", ".c", ".ld", ".s", ".inc", ".cpp", ".hpp", ".py", ".patch")}
        manifest["status"] = "built"
        save()
        if args.mode == "standalone":
            execute(["stdbuf", "-oL", "-eL", str(provider), "run-uncoupled"], env, logs / "simulator.log")
        elif args.mode == "coupled":
            # Let the root orchestration retain its floorplan, port, solver and
            # output controls. Explicit run-local paths override inherited make
            # command-line variables. The software has just been built above.
            execute(["make", "--no-print-directory", "coupled-run", f"RUN_DIR={run}",
                f"RUN_NAME={os.environ.get('RUN_NAME', run.parent.name)}", f"RUN_ROOT={os.environ.get('RUN_ROOT', run.parent.parent)}",
                f"SIMULATOR_CONFIG={manifest['architecture']}", f"SIMULATOR_APP={manifest['app']}",
                "SIMULATOR_PLATFORM=", "BUILD_SIMULATOR=0", f"SOFTHIER_POWER_PROFILE={args.power_profile}",
                f"POWER_INTERVAL_PS={interval}", f"ICE_TARGET_TOP_DIE_CELLS={cells}"], env)
        if args.mode != "build":
            # GVSoC writes this shared file in its working directory. Archive it
            # before another sequential run and verify the memory-port override.
            effective = Path(env.get("SOFTHIER_DIR", ROOT / "SoftHier")) / "gvsoc_config.json"
            shutil.copy2(effective, artifacts / "gvsoc_config.json")
            config = json.loads(effective.read_text())
            vectors, arbiters, networks = [], [], []
            def collect(node):
                if isinstance(node, dict):
                    if node.get("vp_component") == "llm_priority_arbiter":
                        arbiters.append(node)
                    if node.get("vp_component") == "llm_floonoc":
                        networks.append(node)
                    if "vu" in node:
                        vectors.append(node["vu"])
                    for value in node.values():
                        collect(value)
            collect(config)
            if len(vectors) != 64 or any(v["lsu_width"] != 4 for v in vectors):
                raise ValueError("simulator did not apply the 64 Spatz four-byte LSU configuration")
            if len(arbiters) != 16 * 128 or len(networks) != 1:
                raise ValueError("simulator did not apply the bank and collective model fixes")
            manifest["simulator_config_sha256"] = sha256(artifacts / "gvsoc_config.json")
            from validate_run import validate_run
            manifest["validation"] = validate_run(run, args.mode == "coupled", args.regression)
            manifest["status"] = "passed"
        manifest["exit_code"] = 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        manifest["status"] = "failed"
        manifest["error"] = str(error)
        manifest["exit_code"] = getattr(error, "returncode", 1)
        print(f"LLM run failed: {error}", file=sys.stderr)
        return 1
    finally:
        save()
    print(f"LLM {manifest['status']}: {run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
