#!/usr/bin/env python3
"""Propose square symmetries using a static heat-crowding heuristic, not replay."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import numpy as np
from experiments.layout_opt.transforms import (SYMMETRIES, local, plan_for,
    transform_regions, validate_plan, write_json)

NAMES = tuple(SYMMETRIES)


def snapshot(run, output):
    """Freeze completed measured windows into two static load scenarios."""
    run = local(run)
    contract = json.loads((run / "generated/system_config.json").read_text())
    records = []
    for line in (run / "traces/power_hook_trace.jsonl").open():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # The active writer can leave one incomplete last line.
        if record.get("components"):
            records.append(record)
    if not records:
        raise ValueError("no completed power windows")
    ends = np.array([r["window"]["end_ps"] for r in records])
    starts = np.array([r["window"]["start_ps"] for r in records])
    paths = [item["path"] for item in records[0]["components"]]
    values = np.array([[item["total_w"] for item in r["components"]] for r in records])
    duration = ends-starts
    tail = np.maximum(0, ends-np.maximum(starts, ends[-1]-400_000_000))
    means = np.stack([duration @ values/duration.sum(), tail @ values/tail.sum()])
    columns = {item["simulator_path"]: item["power_column"] for item in contract["metadata"]["components"]}
    frozen = dict(schema_version=1, source_run=str(run.relative_to(ROOT)),
        through_ps=int(ends[-1]), completed_windows=len(records),
        complete_execution=records[-1].get("phase") == "final",
        scenarios=["mean_measured_prefix", "mean_last_400us"],
        powers_w={columns[p]: means[:,i].tolist() for i,p in enumerate(paths)},
        limitation="Two static load summaries only; no thermal evolution, repeated activity, or replay.")
    write_json(output, frozen)
    return contract, frozen


def coupling(contract, loads):
    clusters = contract["geometry"]["chip"]["subs"]
    grid = contract["metadata"]["cluster_grid"]
    count = grid["count"]
    width, height = contract["geometry"]["chip"]["shape"]
    scenario_count = len(loads["scenarios"])
    scales = np.array([250., 500., 1000.])
    source_positions, source_sizes, source_powers, targets = [], [], [], []
    for cid in range(count):
        cluster = clusters[f"cluster_{cid}"]
        offset = np.array(cluster["offset"])
        for orientation in NAMES:
            regions = transform_regions(cluster["subs"], cluster["shape"], orientation)
            centers, areas, powers = [], [], []
            for name, region in regions.items():
                centers.append(offset + np.array(region["offset"]) + np.array(region["shape"])/2)
                areas.append(np.prod(region["shape"]))
                powers.append(loads["powers_w"].get(f"chip/cluster_{cid}/{name}", [0.]*scenario_count))
            point_set = list(centers)
            red = regions["redmule"]
            for u in (.1, .5, .9):
                for v in (.1, .5, .9):
                    point_set.append(offset + np.array(red["offset"]) + np.array(red["shape"])*[u,v])
            targets.append(point_set)
            # Fixed DRAM channel position and area; orientation never moves it.
            centers.append(offset + np.array(cluster["shape"])/2)
            areas.append(np.prod(cluster["shape"]))
            powers.append(loads["powers_w"][f"chip/dram3d_chan_{cid}"])
            source_positions.append(centers)
            source_sizes.append(areas)
            source_powers.append(powers)
    targets = np.array(targets)
    target_count, points = targets.shape[:2]
    flat = targets.reshape(-1,2)
    matrix = np.zeros((target_count, points, target_count, len(scales)*scenario_count))
    # A softened distance kernel penalizes nearby heat. Mirror images encode the
    # lack of lateral spreading beyond chip edges. This is a ranking heuristic:
    # it is not calibrated in kelvin and does not solve the 17-layer stack.
    for source in range(target_count):
        centers = np.asarray(source_positions[source])
        soften = .5*np.sqrt(np.asarray(source_sizes[source])/np.pi) + 25.
        weights = np.asarray(source_powers[source])
        field = np.zeros((len(flat), len(scales), scenario_count))
        for image_x in (centers[:,0], -centers[:,0], 2*width-centers[:,0]):
            for image_y in (centers[:,1], -centers[:,1], 2*height-centers[:,1]):
                distance = np.sqrt((flat[:,0,None]-image_x)**2 +
                    (flat[:,1,None]-image_y)**2 + soften**2)
                for index, scale in enumerate(scales):
                    field[:,index,:] += (np.exp(-distance/scale)/distance) @ weights
        matrix[:,:,source,:] = field.reshape(target_count, points, -1)
    return matrix, scales


def optimize(matrix, seed=19, restarts=24):
    count = matrix.shape[0]//8
    rows = np.arange(count)*8
    baseline = rows.copy()
    initial_field = matrix[:,:,baseline,:].sum(axis=2)
    normalization = initial_field[baseline].max(axis=(0,1))

    def score(field, selected):
        peaks = field[selected].max(axis=(0,1))/normalization
        # Worst scenario/length scale is primary; mean breaks near ties.
        return (float(peaks.max()), float(peaks.mean()))

    rng = np.random.default_rng(seed)
    starts = [np.zeros(count, dtype=int)]
    starts += [rng.integers(0,8,size=count) for _ in range(restarts-1)]
    history, best = [], None
    for restart, state in enumerate(starts):
        selected = rows+state
        field = matrix[:,:,selected,:].sum(axis=2)
        current = score(field, selected)
        accepted = []
        for sweep in range(30):
            changed = False
            for cid in rng.permutation(count):
                original = int(selected[cid])
                winner, winning_score = original, current
                for orientation in range(8):
                    candidate = int(rows[cid]+orientation)
                    if candidate == original:
                        continue
                    alternative = selected.copy()
                    alternative[cid] = candidate
                    proposed_field = field + matrix[:,:,candidate,:] - matrix[:,:,original,:]
                    value = score(proposed_field, alternative)
                    if value < winning_score:
                        winner, winning_score = candidate, value
                if winner != original:
                    field += matrix[:,:,winner,:]-matrix[:,:,original,:]
                    selected[cid], current = winner, winning_score
                    accepted.append(dict(sweep=sweep, cluster=int(cid), transform=NAMES[winner%8],
                                         score_ratio=current[0]))
                    changed = True
            if not changed:
                break
        record = dict(restart=restart, score_ratio=current[0], mean_ratio=current[1],
            transforms=[NAMES[index%8] for index in selected], accepted_moves=accepted)
        history.append(record)
        if best is None or current < (best["score_ratio"], best["mean_ratio"]):
            best = record
    return best, history, normalization


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--layout", type=Path, required=True)
    parser.add_argument("--restarts", type=int, default=24)
    args = parser.parse_args()
    output = local(args.output)
    output.mkdir(parents=True, exist_ok=False)
    contract, loads = snapshot(args.run, output / "static_loads.json")
    write_json(output / "source_contract.json", contract)
    if contract["metadata"]["layout_optimization"]["transforms"] != {f"cluster_{i}":"R0" for i in range(16)}:
        raise ValueError("proposal input must be the untransformed baseline")
    matrix, scales = coupling(contract, loads)
    best, history, normalization = optimize(matrix, restarts=args.restarts)
    plan = plan_for(best["transforms"], rationale=
        "Minimize worst static heat-crowding score over two measured load summaries and three spatial scales; validate by fresh full co-simulation.")
    write_json(args.layout, plan)
    comparison = {}
    for path in sorted(local(args.layout).parent.glob("*.json")):
        candidate = json.loads(path.read_text())
        mapping = validate_plan(candidate, contract["metadata"]["cluster_grid"])
        indices = np.array([8*i+NAMES.index(mapping[f"cluster_{i}"]) for i in range(16)])
        field = matrix[:,:,indices,:].sum(axis=2)
        comparison[path.stem] = (field[indices].max(axis=(0,1))/normalization).tolist()
    write_json(output / "search.json", dict(seed=19, restarts=args.restarts,
        scales_um=scales.tolist(), scenarios=loads["scenarios"],
        score_units="dimensionless relative heat-crowding score, NOT a predicted temperature",
        best=best, restarts_history=history, candidate_score_ratios=comparison))
    print(json.dumps(dict(best=best["score_ratio"], layout=str(args.layout), comparisons=comparison),indent=2),flush=True)


if __name__ == "__main__":
    main()
