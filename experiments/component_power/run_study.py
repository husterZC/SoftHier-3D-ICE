#!/usr/bin/env python3
"""Build SDK implementation workloads and run constant/temperature_aware pairs.

Uses implementation/config/arch/arch.py and the SDK's kernel presets. Run
studies sequentially; use analyze.py afterward to make reports and plots.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from prepare_kernel import KERNELS, ROOT, prepare
from Interface_scripts.providers.softhier.floorplans import DEFAULT_RULE, RULES

PROVIDER = ROOT / "Interface_scripts/providers/softhier/provider.sh"


def checked(command, env, log):
    print("Running:", " ".join(str(part) for part in command), flush=True)
    with log.open("w") as stream:
        subprocess.run([str(part) for part in command], cwd=ROOT, env=env,
                       stdout=stream, stderr=subprocess.STDOUT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "Manifest: experiments/component_power/logs/<prefix>/study.json. "
        "Raw runs: runs/component_power/<prefix>_<kernel>_<profile>/. "
        "Existing prefixes/runs are not overwritten; there is no resume mode."))
    workload = parser.add_argument_group("workload")
    workload.add_argument("--kernels", nargs="+", choices=KERNELS, default=list(KERNELS),
        help="norm=RMSNorm, acti=SiLU, gemm=SUMMA GEMM, attn=FlatAttention (default: all four)")
    workload.add_argument("--repeats", type=int, default=1,
        help="positive execution-region repetitions per invocation, not separate runs (default: %(default)s)")
    experiment = parser.add_argument_group("power and thermal settings")
    experiment.add_argument("--interval-ps", type=int, default=1_000_000,
        help="positive coupling interval for norm/acti/gemm in ps; 1000000 ps = 1 us (default: %(default)s)")
    experiment.add_argument("--attention-interval-ps", type=int, default=20_000_000,
        help="positive coupling interval for attn only in ps; overrides --interval-ps for attn (default: %(default)s)")
    experiment.add_argument("--cells", type=int, default=16384,
        help="positive approximate top-die thermal cell target, not an exact count (default: %(default)s)")
    experiment.add_argument("--estimate-scale", type=float, default=1.0,
        help="positive new-logic power coefficient multiplier; RedMulE/SRAM and areas stay fixed (default: %(default)s)")
    experiment.add_argument("--floorplan", choices=tuple(RULES), default=DEFAULT_RULE,
        help="cluster placement: original strip or square three-band layout (default: %(default)s)")
    execution = parser.add_argument_group("build and output")
    execution.add_argument("--prefix", default="implementation",
        help="unique study name for logs, workloads and raw runs; no path separators (default: %(default)s)")
    execution.add_argument("--build-hardware", action="store_true",
        help="also build native GVSoC models before the first kernel; software always builds; not 3D-ICE (default: off)")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.prefix):
        parser.error("prefix must be a simple run name without path separators")
    if min(args.interval_ps, args.attention_interval_ps, args.repeats, args.cells) <= 0:
        parser.error("intervals, repetitions, and cell count must be positive")
    if not math.isfinite(args.estimate_scale) or args.estimate_scale <= 0:
        parser.error("estimate scale must be finite and positive")
    if len(set(args.kernels)) != len(args.kernels):
        parser.error("kernels must not be repeated in the list")
    study = ROOT / "experiments/component_power"
    sdk = ROOT / "SoftHier/soft_hier_sdk"
    logs = study / "logs" / args.prefix
    logs.mkdir(parents=True, exist_ok=False)
    # Bash can continue reading a script after its child finishes. Freeze the
    # provider so an unrelated source edit cannot corrupt a running shell.
    frozen_provider = logs / "provider.sh"
    shutil.copy2(PROVIDER, frozen_provider)
    manifest = {"prefix": args.prefix, "interval_ps": args.interval_ps,
                "attention_interval_ps": args.attention_interval_ps, "repeats": args.repeats,
                "provider_sha256": hashlib.sha256(frozen_provider.read_bytes()).hexdigest(),
                "target_cells": args.cells, "estimate_scale": args.estimate_scale,
                "floorplan_rule": args.floorplan, "runs": []}
    manifest_path = logs / "study.json"
    for index, kernel in enumerate(args.kernels):
        output = study / "workloads" / args.prefix / kernel
        output.mkdir(parents=True, exist_ok=True)
        arch = output / "arch.py"
        source_arch = sdk / "implementation/config/arch/arch.py"
        arch.write_text(source_arch.read_text() + "\n" +
            "_power_original_init = FlexClusterArch.__init__\n" +
            "def _power_init(self):\n    _power_original_init(self)\n" +
            f"    setattr(self, 'power_estimate_scale', {args.estimate_scale!r})\n" +
            "FlexClusterArch.__init__ = _power_init\n")
        workload = prepare(sdk, kernel, output, arch, sdk / f"implementation/config/kernels/{kernel}.py", args.repeats)
        env = os.environ.copy()
        env.update(SIMULATOR_CONFIG=str(arch), SIMULATOR_APP=workload["app"],
                   SOFTHIER_FLOORPLAN=args.floorplan,
                   SIMULATOR_PLATFORM=workload["preload"], SOFTHIER_SW_BUILD=str(output / "sw_build"),
                   SIMULATOR_PROVIDER=str(frozen_provider), SOFTHIER_PROVIDER_SCRIPT_DIR=str(PROVIDER.parent))
        action = "build" if args.build_hardware and index == 0 else "build-workload"
        checked(["bash", frozen_provider, action], env, logs / f"{kernel}_build.log")
        binary = output / "sw_build/softhier.elf"
        binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
        for profile in ("constant", "temperature_aware"):
            interval_ps = args.attention_interval_ps if kernel == "attn" else args.interval_ps
            run_id = f"{args.prefix}_{kernel}_{profile}"
            run_dir = ROOT / "runs/component_power" / run_id
            if run_dir.exists():
                raise ValueError(f"refusing to overwrite run: {run_dir}")
            command = ["make", "coupled-run", "RUN_NAME=component_power", f"RUN_ID={run_id}",
                "BUILD_SIMULATOR=0", "BUILD_3DICE=0", f"SOFTHIER_POWER_PROFILE={profile}",
                f"POWER_INTERVAL_PS={interval_ps}", f"ICE_STEP_SECONDS={interval_ps * 1e-13:.12g}",
                f"ICE_TARGET_TOP_DIE_CELLS={args.cells}"]
            checked(command, env, logs / f"{kernel}_{profile}.log")
            artifact = run_dir / "artifacts"
            artifact.mkdir()
            shutil.copy2(binary, artifact / binary.name)
            shutil.copy2(workload["preload"], artifact / "preload.elf")
            shutil.copy2(output / "workload.json", artifact / "workload.json")
            shutil.copy2(arch, artifact / "arch.py")
            shutil.copy2(frozen_provider, artifact / "provider.sh")
            if hashlib.sha256(binary.read_bytes()).hexdigest() != binary_hash:
                raise ValueError("workload binary changed during paired run")
            manifest["runs"].append({"kernel": kernel, "profile": profile,
                "interval_ps": interval_ps, "repeats": args.repeats,
                "path": str(run_dir.relative_to(ROOT)), "binary_sha256": binary_hash,
                "preload_sha256": workload["preload_sha256"]})
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Completed study manifest: {manifest_path}", flush=True)


if __name__ == "__main__":
    main()
