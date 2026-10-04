#!/usr/bin/env python3
"""Stage the SDK runtime without changing its generated, tracked headers."""

import argparse
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys


def copy_tree(source, destination):
    """Overlay regular SDK files; compatible with the site's older Python 3."""
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = destination / child.name
        if child.is_dir():
            copy_tree(child, target)
        else:
            shutil.copy2(child, target)


def stage(sdk, workdir, arch):
    # The upstream generator locates its output relative to __file__. Preserve
    # that layout in the build directory instead of modifying the SDK checkout.
    snapshot = workdir / "sdk_snapshot" / "soft_hier_sdk"
    runtime = snapshot / "runtime"
    copy_tree(sdk / "runtime", runtime)
    utility = snapshot / "utilities" / "config.py"
    utility.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(sdk / "utilities" / "config.py", utility)
    subprocess.run([sys.executable, str(utility), str(arch)], check=True)
    spec = importlib.util.spec_from_file_location("workload_arch", arch)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = module.FlexClusterArch()
    isa = "rv32imafdv_zfh" if config.spatz_attaced_core_list else "rv32imafd_zfh"
    return runtime, isa


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--arch", type=Path, required=True)
    args = parser.parse_args()
    runtime, isa = stage(args.sdk.resolve(), args.workdir.resolve(), args.arch.resolve())
    print(f"Staged SDK runtime: {runtime}; ISA: {isa}")
    (args.workdir / "riscv_arch.txt").write_text(isa + "\n")
