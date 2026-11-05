#!/usr/bin/env python3
"""Compare complete, identical prefixes of ongoing native runs; not final scores."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import numpy as np
from experiments.layout_opt.transforms import local, write_json


def complete_lines(path):
    with local(path).open() as stream:
        for line in stream:
            if not line.endswith("\n"):
                break
            if line.strip() and not line.startswith(("#", "%")):
                yield line


def load_prefix(path):
    path = local(path)
    records = [json.loads(line) for line in complete_lines(path / "traces/power_hook_trace.jsonl")]
    records = [r for r in records if r.get("components")]
    series = {}
    for die in ("logic", "dram"):
        series[die] = np.array([max(map(float, line.split()))-273.15 for line in
            complete_lines(path / f"results/3dice/output_{die}_die.txt")])
    count = min(len(records), *(len(values) for values in series.values()))
    return records[:count], {key: values[:count] for key, values in series.items()}


def compare(runs, output):
    loaded = [load_prefix(path) for path in runs]
    count = min(len(records) for records, _ in loaded)
    if count == 0:
        raise ValueError("no common completed thermal slot")
    reference = loaded[0][0]
    results = []
    for path, (records, series) in zip(runs, loaded):
        delta = 0.
        for a, b in zip(reference[:count], records[:count]):
            if a["window"] != b["window"]:
                raise ValueError("power windows differ")
            left, right = ({i["path"]: i for i in r["components"]} for r in (a,b))
            if left.keys() != right.keys():
                raise ValueError("component inventories differ")
            for component in left:
                av, bv = left[component]["dynamic_w"], right[component]["dynamic_w"]
                if not np.isclose(av, bv, rtol=1e-6, atol=1e-10):
                    raise ValueError("dynamic workload differs: " + component)
                delta = max(delta, abs(av-bv))
        results.append(dict(run=str(local(path).relative_to(ROOT)),
            max_dynamic_delta_w=delta,
            peak_through_prefix_c=max(float(values[:count].max()) for values in series.values()),
            latest_cell_peak_c={key: float(values[count-1]) for key, values in series.items()}))
    result = dict(status="PRELIMINARY: identical completed prefixes, not full-run scores",
        slots=count, through_us=reference[count-1]["window"]["end_ps"]*1e-6, runs=results)
    if output:
        write_json(output, result)
    print(json.dumps(result, indent=2), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    compare(args.runs, args.output)


if __name__ == "__main__":
    main()
