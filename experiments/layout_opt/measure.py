#!/usr/bin/env python3
"""Validate real co-simulation outputs and extract comparable layout metrics."""

import argparse
import csv
import importlib.util
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "experiments/layout_opt"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments/llm_dram3d"))
import numpy as np
from experiments.layout_opt.transforms import local, write_json


def analysis_module():
    spec = importlib.util.spec_from_file_location("layout_llm_analysis", ROOT / "experiments/llm_dram3d/analyze.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rectangles(contract):
    result = {}
    for cluster_name, cluster in contract["geometry"]["chip"]["subs"].items():
        for name, region in cluster["subs"].items():
            result[f"chip/{cluster_name}/{name}"] = [
                cluster["offset"][0] + region["offset"][0],
                cluster["offset"][1] + region["offset"][1], *region["shape"]]
    return result


def roi_and_moe(run, timing):
    events = []
    for path in sorted((run / "timing").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            event = json.loads(line)
            if event.get("actor") == "gvsoc" and event.get("phase") == "roi_start":
                events.append(event)
    if len(events) != 1:
        raise ValueError("expected one measured layer-start marker")
    start = events[0]["sim_ps"]
    stages = timing["stage_cycles"]
    order = ("norm1", "qkv", "rope_kv_append", "attention", "out_projection",
             "attention_residual", "norm2", "router_projection", "topk_softmax")
    moe_start = start + sum(stages[name] for name in order) * 1000
    return start, (moe_start, moe_start + stages["experts_swiglu"]*1000)


def measure_run(common, llm, entry, study, output):
    path = local(ROOT / entry["path"])
    run = common.BASE.load_run(path)
    contract = json.loads((path / "generated/system_config.json").read_text())
    manifest = llm.check_artifacts(entry, run, study)
    llm.validate_thermal_physics(common, run, contract, manifest)
    common.validate_inventory(run)
    llm.validate_logic_leakage(common, run, llm.die_paths(contract))
    for filename, digest in study["layout_source_sha256"].items():
        if llm.sha256(local(path / "artifacts/layout_implementation" / filename)) != digest:
            raise ValueError("archived layout implementation differs from study")
    if contract["metadata"]["layout_optimization"]["layout_sha256"] != study["layout_sha256"]:
        raise ValueError("run layout identity differs from study")
    if json.loads((path / "artifacts/layout.json").read_text()) != study["layout"]:
        raise ValueError("archived layout differs from study")
    timing = entry["numerical_validation"]
    _, moe = roi_and_moe(path, timing)
    records = run.power_records
    paths = [item["path"] for item in records[0]["components"]]
    columns = {item["simulator_path"]: item["power_column"] for item in run.metadata["components"]}
    starts = np.array([r["window"]["start_ps"] for r in records], dtype=np.int64)
    ends = np.array([r["window"]["end_ps"] for r in records], dtype=np.int64)
    dynamic = np.array([[i["dynamic_w"] for i in r["components"]] for r in records])
    leakage = np.array([[i["leakage_w"] for i in r["components"]] for r in records])
    duration = (ends-starts)*1e-12
    moe_overlap = np.maximum(0, np.minimum(ends, moe[1])-np.maximum(starts, moe[0]))*1e-12
    if not np.isclose(moe_overlap.sum(), (moe[1]-moe[0])*1e-12, rtol=1e-9):
        raise ValueError("power trace does not cover the full measured MoE stage")
    averages = (dynamic+leakage).T @ duration / duration.sum()
    dynamic_average = dynamic.T @ duration / duration.sum()
    moe_average = (dynamic+leakage).T @ moe_overlap / moe_overlap.sum()
    moe_dynamic = dynamic.T @ moe_overlap / moe_overlap.sum()
    temperature_max = {p: -math.inf for p in paths}
    temperature_time = {}
    for row in run.temperatures:
        p = row["path"]
        if row["temperature_c"] > temperature_max[p]:
            temperature_max[p] = row["temperature_c"]
            temperature_time[p] = row["end_ps"]
    power = {columns[p]: dict(path=p, average_total_w=float(averages[i]),
        average_dynamic_w=float(dynamic_average[i]), moe_average_total_w=float(moe_average[i]),
        moe_average_dynamic_w=float(moe_dynamic[i]), peak_component_c=temperature_max[p],
        component_peak_end_ps=temperature_time[p]) for i, p in enumerate(paths)}
    boxes = rectangles(contract)
    grid = contract["metadata"]["cluster_grid"]
    side = contract["geometry"]["chip"]["subs"]["cluster_0"]["shape"][0]
    dice = path / "results/3dice"
    dies, cell_series = {}, {}
    for die in manifest["dies"]:
        geometry = common.VIEW.load_tmap_geometry_cells(local(dice / die["coordinates_output"]))
        cells = np.asarray(geometry["cells"])
        centers = cells[:, :2]+cells[:, 2:]/2
        clusters = np.minimum((centers[:, 0]/side).astype(int), grid["columns"]-1) + grid["columns"]*np.minimum((centers[:, 1]/side).astype(int), grid["rows"]-1)
        peak, low, peak_slot, peak_cell = -math.inf, math.inf, None, None
        series = []
        per_cluster = np.full(grid["count"], -np.inf)
        indices = [np.flatnonzero(clusters == i) for i in range(grid["count"])]
        with local(dice / die["map_output"]).open() as stream:
            for line in stream:
                if not line.strip() or line.lstrip().startswith(("%", "#")):
                    continue
                values = np.fromstring(line, sep=" ")
                if len(values) != len(cells) or not np.isfinite(values).all() or values.min() < 299.999:
                    raise ValueError("invalid or sub-ambient native cell temperature")
                index = int(values.argmax())
                if values[index] > peak:
                    peak, peak_slot, peak_cell = float(values[index]), len(series), index
                low = min(low, float(values.min()))
                per_cluster = np.maximum(per_cluster, [values[ix].max() for ix in indices])
                series.append(float(values.max())-273.15)
        if len(series) != len(records):
            raise ValueError("native temperature slot count differs from actual power windows")
        point = centers[peak_cell].tolist()
        component = None
        if die["id"] == "LOGIC_DIE":
            matches = [name for name, (x, y, w, h) in boxes.items()
                       if x <= point[0] < x+w and y <= point[1] < y+h]
            if len(matches) != 1:
                raise ValueError("peak cell does not map uniquely to a component")
            component = matches[0]
        dies[die["id"]] = dict(peak_cell_c=peak-273.15, minimum_cell_c=low-273.15,
            cell_count=len(cells), slots=len(series), peak_slot=peak_slot,
            peak_thermal_time_us=(peak_slot+1)*study["interval_ps"]*1e-6,
            peak_power_window_end_ps=int(ends[peak_slot]), peak_position_um=point,
            peak_component=component, per_cluster_peak_cell_c=(per_cluster-273.15).tolist())
        cell_series[die["id"]] = np.array(series)
    red = [power[f"chip/cluster_{i}/redmule"] for i in range(grid["count"])]
    red_moe = np.array([r["moe_average_dynamic_w"] for r in red])
    result = dict(profile=run.profile, run=str(path.relative_to(ROOT)),
        peak_cell_c=max(d["peak_cell_c"] for d in dies.values()),
        peak_component_c=max(temperature_max.values()), dies=dies, components=power,
        layer_time_us=timing["layer_time_us"], stage_cycles=timing["stage_cycles"],
        duration_us=float(ends[-1]*1e-6), slots=len(records),
        dynamic_energy_j=float((dynamic.sum(axis=1)*duration).sum()),
        leakage_energy_j=float((leakage.sum(axis=1)*duration).sum()),
        moe_bounds_ps=list(moe), moe_window_integration="slot means weighted by phase overlap; 20 us boundary resolution",
        redmule_moe_dynamic_mean_w=float(red_moe.mean()),
        redmule_moe_dynamic_min_w=float(red_moe.min()), redmule_moe_dynamic_max_w=float(red_moe.max()),
        redmule_moe_dynamic_cv=float(red_moe.std()/red_moe.mean()),
        redmule_peak_spread_c=max(r["peak_component_c"] for r in red)-min(r["peak_component_c"] for r in red),
        binary_sha256=entry["binary_sha256"], preload_sha256=entry["preload_sha256"])
    output.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(output / f"{run.profile}_activity.npz", start_ps=starts, end_ps=ends,
        paths=np.array(paths), dynamic_w=dynamic,
        dram_background_w=leakage[:, [i for i,p in enumerate(paths) if "/dram3d_chan_" in p]])
    np.savez_compressed(output / f"{run.profile}_temperature.npz", end_ps=ends,
        **cell_series)
    return result, run, contract


def measure(trial, output):
    trial, output = local(trial), local(output)
    study = json.loads((trial / "study.json").read_text())
    if study.get("thermal_replay") is not None or study.get("thermal_replay_till") is not None:
        raise ValueError("thermal replay is forbidden in layout optimization")
    if {r["profile"] for r in study["runs"]} != {"constant", "temperature_aware"}:
        raise ValueError("both complete default leakage profiles are required")
    llm = analysis_module()
    common = llm.load_common()
    loaded, contracts, measured = {}, {}, {}
    for entry in study["runs"]:
        profile = entry["profile"]
        measured[profile], loaded[profile], contracts[profile] = measure_run(common, llm, entry, study, output)
    for field in ("binary_sha256", "preload_sha256", "stage_cycles", "layer_time_us"):
        if measured["constant"][field] != measured["temperature_aware"][field]:
            raise ValueError("paired workload differs: " + field)
    llm.validate_pair(loaded["constant"], loaded["temperature_aware"],
                      [contracts["constant"], contracts["temperature_aware"]])
    result = dict(schema_version=1, trial=str(trial.relative_to(ROOT)), name=trial.name,
        objective="maximum native thermal-cell temperature across both dies and all slots; temperature_aware",
        layout=study["layout"], layout_sha256=study["layout_sha256"],
        profiles=measured, score_c=measured["temperature_aware"]["peak_cell_c"])
    write_json(output / "metrics.json", result)
    with (output / "redmule.csv").open("w") as stream:
        writer = csv.writer(stream)
        writer.writerow(["profile", "cluster", "transform", "peak_component_c", "average_dynamic_w",
                         "moe_average_dynamic_w", "moe_average_total_w"])
        for profile, measurement in measured.items():
            for cid in range(16):
                item = measurement["components"][f"chip/cluster_{cid}/redmule"]
                writer.writerow([profile, cid, study["layout"]["transforms"][f"cluster_{cid}"],
                    *[item[key] for key in ("peak_component_c", "average_dynamic_w",
                                           "moe_average_dynamic_w", "moe_average_total_w")]])
    print(json.dumps({"name": result["name"], "score_c": result["score_c"],
        "redmule_spread_c": measured["temperature_aware"]["redmule_peak_spread_c"],
        "moe_dynamic_cv": measured["constant"]["redmule_moe_dynamic_cv"]}, indent=2), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    measure(args.trial, args.output)


if __name__ == "__main__":
    main()
