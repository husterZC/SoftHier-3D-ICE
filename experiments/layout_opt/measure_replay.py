#!/usr/bin/env python3
"""Validate completed replays and scan every native active-die cell and slot."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import numpy as np
from experiments.layout_opt.replay_study import CAMPAIGN, HERE, NAMES, check_complete
from experiments.layout_opt.transforms import local, write_json
from experiments.layout_opt.measure import rectangles
from experiments.thermal_replay.activity import digest
from experiments.thermal_replay.analyze import ReplayRun, VIEW, csv_file


def scan_die(directory, die, slots, slot_ps, output):
    """Stream dense output; retain exact extrema and indexed animation samples."""
    directory, output = local(directory), local(output)
    geometry = VIEW["load_tmap_geometry_cells"](local(directory / die["coordinates_output"]))
    cells = np.asarray(geometry["cells"])
    count = len(cells)
    stride = max(1, round(10_000_000_000 / slot_ps))
    sampled = sorted(set([0, *range(stride-1, slots, stride), slots-1]))
    wanted = set(sampled)
    frames = {}
    series = np.empty(slots)
    offsets = np.empty(slots, dtype=np.int64)
    maximum, minimum = -np.inf, np.inf
    peak_slot = peak_cell = None
    hasher = hashlib.sha256()
    path = local(directory / die["map_output"])
    with path.open("rb") as stream:
        index = 0
        while True:
            offset = stream.tell()
            line = stream.readline()
            if not line:
                break
            hasher.update(line)
            if not line.strip() or line.lstrip().startswith((b"%", b"#")):
                continue
            values = np.fromstring(line, sep=" ")-273.15
            if index >= slots or len(values) != count or not np.isfinite(values).all():
                raise ValueError(f"invalid native cell row in {path}, slot {index}")
            if values.min() < 26.849:
                raise ValueError("subambient native thermal solution")
            cell = int(values.argmax())
            series[index] = values[cell]
            offsets[index] = offset
            minimum = min(minimum, float(values.min()))
            if values[cell] > maximum:
                maximum, peak_slot, peak_cell = float(values[cell]), index, cell
                peak_frame = values.copy()
            if index in wanted:
                frames[index] = values.copy()
            index += 1
            if index % 10000 == 0:
                print(f"  {die['id']}: scanned {index}/{slots} native slots", flush=True)
    if index != slots:
        raise ValueError(f"native cell history has {index} slots, expected {slots}")
    frames[peak_slot] = peak_frame
    frame_slots = sorted(frames)
    point = cells[peak_cell,:2] + cells[peak_cell,2:]/2
    output.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(output / f"{die['id'].lower()}.npz", cells=cells,
        time_ps=(np.arange(slots,dtype=np.int64)+1)*slot_ps, peak_c=series,
        row_offsets=offsets, frame_slots=frame_slots,
        frames_c=np.array([frames[i] for i in frame_slots]))
    return dict(cell_count=count, slots=slots, minimum_cell_c=minimum,
        peak_cell_c=maximum, peak_slot=peak_slot, peak_time_s=(peak_slot+1)*slot_ps*1e-12,
        final_peak_cell_c=float(series[-1]), peak_cell=peak_cell,
        peak_position_um=point.tolist(), map_file=str(path.relative_to(ROOT)),
        map_sha256=hasher.hexdigest(),
        coordinates_sha256=digest(directory / die["coordinates_output"]))


def measure(campaign, name, profile):
    campaign_path = local(HERE / "artifacts" / campaign)
    study = json.loads((campaign_path / "study.json").read_text())
    entry = next(item for item in study["layouts"] if item["name"] == name)
    run_path = campaign_path / "runs" / name / profile
    summary = check_complete(run_path, study, entry, profile)
    output = local(HERE / "results" / campaign / name / profile)
    output.mkdir(parents=True, exist_ok=True)
    result_file = output / "metrics.json"
    if result_file.exists():
        cached = json.loads(result_file.read_text())
        if cached["summary_sha256"] != digest(run_path / "summary.json"):
            raise ValueError("cached measurement summary differs from the run")
        return cached
    replay = ReplayRun(run_path, profile)
    if not np.isclose(replay.h,study["heat_transfer_coefficient_w_m2_k"],rtol=0,atol=1e-8) or replay.n != 50000:
        raise ValueError("replay settings do not match this study")
    metrics = dict(name=name, profile=profile, run=str(run_path.relative_to(ROOT)),
        summary_sha256=digest(run_path / "summary.json"),
        analysis_sha256=digest(__file__), component_metrics=replay.metrics,
        dynamic_sha256=replay.dynamic_sha256, dram_background_sha256=replay.dram_background_sha256,
        initial_temperature_c=float(replay.initial_c.max()),
        model_limit_first_seconds=summary["first_model_limit_seconds"],
        dies={})
    boxes = rectangles(replay.document)
    for die in replay.layout.dies:
        native = scan_die(run_path,die,replay.n,replay.slot_ps,output)
        if die["id"] == "LOGIC_DIE":
            x,y = native["peak_position_um"]
            matches = [name for name,(a,b,w,h) in boxes.items() if a <= x < a+w and b <= y < b+h]
            if len(matches) != 1:
                raise ValueError("native hotspot does not map to a unique logic component")
            native["peak_component"] = matches[0]
        native["component_metrics"] = replay.die_metrics[die["id"]]
        metrics["dies"][die["id"]] = native
    metrics["peak_cell_c"] = max(d["peak_cell_c"] for d in metrics["dies"].values())
    red_indices = [replay.paths.index(f"/chip/cluster_{i}/redmule") for i in range(16)]
    metrics["redmule_component_peaks_c"] = replay.component_peak_c[red_indices].tolist()
    metrics["redmule_peak_spread_c"] = float(np.ptp(replay.component_peak_c[red_indices]))
    csv_file(output / "validation.csv",replay.validation)
    write_json(output / "component_metrics.json",replay.metrics)
    write_json(result_file,metrics)
    print(f"Measured {name}/{profile}: native peak {metrics['peak_cell_c']:.3f} C",flush=True)
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign",default=CAMPAIGN)
    parser.add_argument("--name",choices=NAMES,required=True)
    parser.add_argument("--profile",choices=("constant","temperature_aware"),required=True)
    args = parser.parse_args()
    measure(args.campaign,args.name,args.profile)


if __name__ == "__main__":
    main()
