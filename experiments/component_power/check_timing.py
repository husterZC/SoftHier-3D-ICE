#!/usr/bin/env python3
"""Rerun archived RMSNorm and SiLU binaries without power/thermal instrumentation.

Requires a manifest containing both norm and acti constant runs. This launches
GVSoC (not 3D-ICE); run sequentially with other studies because DRAMSys files
are shared. It does not rebuild the simulator or workloads.
"""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def reported_duration(text):
    values = {int(v) for v in re.findall(r"Total Time:\s+(\d+) ns", text)}
    if len(values) != 1:
        raise ValueError("missing or inconsistent DRAMSys completion time")
    return values.pop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True,
        help="one study.json containing one constant run each for norm and acti; other kernels are ignored")
    parser.add_argument("--logs", type=Path, required=True,
        help="new directory for the frozen provider and uncoupled simulator logs; must not exist")
    parser.add_argument("--output", type=Path, required=True,
        help="timing comparison CSV file (not a directory); an existing file is overwritten")
    args = parser.parse_args()
    args.logs.mkdir(parents=True, exist_ok=False)
    provider = ROOT / "Interface_scripts/providers/softhier/provider.sh"
    frozen = args.logs.resolve() / "provider.sh"
    shutil.copy2(provider, frozen)
    rows = []
    for entry in json.loads(args.manifest.read_text())["runs"]:
        if entry["profile"] != "constant" or entry["kernel"] not in ("norm", "acti"):
            continue
        run = ROOT / entry["path"]
        artifact = run / "artifacts"
        for name, key in (("softhier.elf", "binary_sha256"), ("preload.elf", "preload_sha256")):
            if hashlib.sha256((artifact / name).read_bytes()).hexdigest() != entry[key]:
                raise ValueError(f"archived artifact changed: {name}")
        env = dict(os.environ, SIMULATOR_CONFIG=str(artifact / "arch.py"),
            SIMULATOR_PLATFORM=str(artifact / "preload.elf"), SOFTHIER_SW_BUILD=str(artifact),
            SOFTHIER_PROVIDER_SCRIPT_DIR=str(provider.parent), SOFTHIER_POWER_PROFILE="constant")
        log = args.logs / (entry["kernel"] + ".log")
        with log.open("w") as stream:
            subprocess.run(["bash", str(frozen), "run-uncoupled"], cwd=ROOT, env=env,
                           stdout=stream, stderr=subprocess.STDOUT, check=True)
        coupled_ns = reported_duration((run / "logs/simulator.log").read_text())
        uncoupled_ns = reported_duration(log.read_text())
        if coupled_ns != uncoupled_ns:
            raise ValueError(f"instrumentation changed execution time: {coupled_ns} vs {uncoupled_ns} ns")
        rows.append(dict(kernel=entry["kernel"], coupled_ns=coupled_ns, uncoupled_ns=uncoupled_ns,
                         difference_ns=uncoupled_ns - coupled_ns, binary_sha256=entry["binary_sha256"]))
    if len(rows) != 2:
        raise ValueError("manifest must contain one constant run for RMSNorm and SiLU")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Uncoupled timing checks passed: {args.output}")


if __name__ == "__main__":
    main()
