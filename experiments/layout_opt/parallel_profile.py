#!/usr/bin/env python3
"""Start the aware profile while an existing trial's constant profile finishes.

The original sequential controller will archive its constant run, then stop at
its existing-directory guard. This controller owns the aware directory, waits
for that constant archive if necessary, and merges both completed entries.
No native process is stopped and no simulation settings are changed.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from experiments.layout_opt import run as runner
from experiments.layout_opt.transforms import local, write_json


def start(campaign,name):
    trial=local(ROOT/"experiments/layout_opt/artifacts"/campaign/"trials"/name)
    study=trial/"study.json"
    original=json.loads(study.read_text())
    constant=trial/"runs/constant"
    if not constant.is_dir() or (trial/"runs/temperature_aware").exists():
        raise ValueError("requires a started constant run and no aware run")
    orchestration=trial/"orchestration"
    orchestration.mkdir(exist_ok=False)
    shutil.copy2(__file__,orchestration/"parallel_profile.py")
    provenance=dict(controller="parallel_profile.py",sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        reason="Run independent leakage profiles concurrently without changing native settings.",
        original_controller="Expected to stop at its existing-directory guard after archiving constant.")
    write_json(orchestration/"mode.json",provenance)

    def merge_completed(path,value):
        if local(path) != study:
            return write_json(path,value)
        # The original controller owns the constant entry. Wait for that write
        # so neither controller can overwrite a completed entry from the other.
        while True:
            current=json.loads(study.read_text())
            if any(entry["profile"]=="constant" for entry in current["runs"]):
                break
            time.sleep(2)
        for key in original.keys()-{"runs"}:
            if current[key] != original[key] or value[key] != original[key]:
                raise ValueError("trial settings changed during parallel execution")
        entries={entry["profile"]:entry for entry in current["runs"]}
        for entry in value["runs"]:
            if entry["profile"] in entries and entries[entry["profile"]] != entry:
                raise ValueError("conflicting completed profile entries")
            entries[entry["profile"]]=entry
        value=dict(value,runs=[entries[p] for p in ("constant","temperature_aware")],
                   parallel_orchestration=provenance)
        write_json(path,value)

    runner.write_json=merge_completed
    runner.base.PROFILES=("temperature_aware",)
    runner.run(campaign,name,trial/"layout.json")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign",default="b8_opt26_h25000")
    parser.add_argument("--name",required=True)
    args=parser.parse_args()
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*",v) for v in (args.campaign,args.name)):
        parser.error("campaign and name must be simple directory names")
    start(args.campaign,args.name)


if __name__ == "__main__":
    main()
