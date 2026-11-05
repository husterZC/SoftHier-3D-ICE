#!/usr/bin/env python3
"""Measure finished replay jobs, then render the complete four-layout comparison."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from experiments.layout_opt.replay_study import CAMPAIGN, HERE, NAMES, PROFILES
from experiments.layout_opt.transforms import local, write_json


def finish(campaign):
    folder = local(HERE / "artifacts" / campaign)
    pending = {(name,profile) for name in NAMES for profile in PROFILES}
    active, done, failures = {}, [], []

    def measure(name,profile):
        with (folder / "logs" / f"measure_{name}_{profile}.log").open("w") as log:
            subprocess.run([sys.executable,str(HERE / "measure_replay.py"),
                "--campaign",campaign,"--name",name,"--profile",profile],
                cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,check=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        while pending or active:
            for key in list(pending):
                name,profile = key
                path = folder / "runs" / name / profile / "summary.json"
                if not path.is_file():
                    continue
                try:
                    summary = json.loads(path.read_text())
                except json.JSONDecodeError:
                    continue
                if summary["status"] == "running":
                    continue
                pending.remove(key)
                if summary["status"] != "time_limit":
                    failures.append(dict(name=name,profile=profile,error=summary["status"]))
                else:
                    active[pool.submit(measure,name,profile)] = key
                    print(f"Measuring {name}/{profile}",flush=True)
            for future in list(active):
                if not future.done():
                    continue
                name,profile = active.pop(future)
                try:
                    future.result()
                    done.append(dict(name=name,profile=profile))
                    print(f"Validated {name}/{profile}",flush=True)
                except Exception as exc:
                    failures.append(dict(name=name,profile=profile,error=str(exc)))
            write_json(folder / "analysis_progress.json",dict(completed=done,failures=failures,
                pending=[list(key) for key in sorted(pending)],active=[list(key) for key in active.values()]))
            if pending or active:
                time.sleep(15)
    if failures:
        raise RuntimeError(f"replay or measurement failures: {failures}")
    with (folder / "logs/final_comparison.log").open("w") as log:
        subprocess.run([sys.executable,str(HERE / "compare_replay.py"),
            "--campaign",campaign,"--animations"],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,check=True)
    print("All eight replays measured; four-layout figures and GIFs generated.",flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign",default=CAMPAIGN)
    args = parser.parse_args()
    finish(args.campaign)


if __name__ == "__main__":
    main()
