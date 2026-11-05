#!/usr/bin/env python3
"""Inspect completed records in ongoing layout trials without changing runs."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from experiments.layout_opt.transforms import local, write_json
from experiments.layout_opt.config import CAMPAIGN


def tail_lines(path, size=2*1024*1024):
    if not path.is_file():
        return []
    with path.open("rb") as stream:
        stream.seek(0, 2)
        end = stream.tell()
        start = max(0, end-size)
        stream.seek(start)
        data = stream.read().splitlines(keepends=True)
    if start:
        data = data[1:]
    return [line.decode() for line in data if line.endswith(b"\n")]


def status(campaign):
    folder = local(ROOT / "experiments/layout_opt/artifacts" / campaign / "trials")
    result = []
    if not folder.is_dir():
        return result
    for trial in sorted(folder.iterdir()):
        if not (trial / "study.json").is_file():
            continue
        study = json.loads((trial / "study.json").read_text())
        complete = {entry["profile"] for entry in study["runs"]}
        for profile in ("constant", "temperature_aware"):
            run = trial / "runs" / profile
            if not run.exists():
                continue
            record = None
            for line in reversed(tail_lines(run / "traces/power_hook_trace.jsonl")):
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("components"):
                    record = item
                    break
            state = "complete" if profile in complete else "running"
            if not record and "Error" in "".join(tail_lines(trial / f"{profile}.log", 4096)):
                state = "failed before power capture"
            item = dict(trial=trial.name, profile=profile, state=state,
                simulated_us=record["window"]["end_ps"]*1e-6 if record else 0.)
            for die in ("logic", "dram"):
                for line in reversed(tail_lines(run / f"results/3dice/output_{die}_die.txt")):
                    if not line.strip() or line.startswith(("%", "#")):
                        continue
                    item[f"latest_{die}_cell_peak_c"] = max(map(float, line.split()))-273.15
                    break
            result.append(item)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", default=CAMPAIGN)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = status(args.campaign)
    if args.output:
        write_json(args.output, result)
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
