#!/usr/bin/env python3
"""Propose a top-row refinement using observed hotspots and static spatial scores.

No power trace is replayed and no temperatures are integrated. Per-cluster
static-score ratios are anchored to one complete measured control run only to
rank candidate changes. Every selected candidate still needs full simulations.
"""

import argparse
import itertools
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import numpy as np
from experiments.layout_opt.search import coupling,NAMES
from experiments.layout_opt.transforms import local,plan_for,write_json


def refine(load_folder,measurement,layout,output,destination):
    load_folder,output=local(load_folder),local(output)
    contract=json.loads((load_folder/"source_contract.json").read_text())
    loads=json.loads((load_folder/"static_loads.json").read_text())
    measured=json.loads(local(measurement).read_text())
    if "profiles" in measured:
        measured=measured["profiles"]["constant"]
    current=json.loads(local(layout).read_text())
    if measured["profile"] != "constant" or not loads["complete_execution"]:
        raise ValueError("requires complete constant-profile observations")
    measured_contract=json.loads(local(ROOT/measured["run"]/"generated/system_config.json").read_text())
    if measured_contract["metadata"]["layout_optimization"]["transforms"] != current["transforms"]:
        raise ValueError("measurement and starting layout differ")
    matrix,scales=coupling(contract,loads)
    rows=np.arange(16)*8
    initial=np.array([8*i+NAMES.index(current["transforms"][f"cluster_{i}"]) for i in range(16)])
    initial_field=matrix[:,:,initial,:].sum(axis=2)
    initial_peaks=initial_field[initial].max(axis=1)
    rise=np.array(measured["dies"]["LOGIC_DIE"]["per_cluster_peak_cell_c"])-26.85
    fixed_field=matrix[:,:,initial[:12],:].sum(axis=2)
    records=[]
    best=None
    for states in itertools.product(range(8),repeat=4):
        selected=initial.copy()
        selected[12:]=rows[12:]+states
        # Ratios are evaluated in every scenario and length scale; take the
        # most pessimistic inferred change for each cluster, including neighbors.
        field=fixed_field[selected]+matrix[selected][:,:,selected[12:],:].sum(axis=2)
        ratio=field.max(axis=1)/initial_peaks
        scaled=rise[:,None]*ratio
        score=float(scaled.max()/rise.max())
        mean=float(scaled.mean()/rise.mean())
        record=dict(top_row=[NAMES[s] for s in states],score_ratio=score,mean_ratio=mean,
                    worst_cluster=int(np.unravel_index(scaled.argmax(),scaled.shape)[0]))
        records.append(record)
        if best is None or (score,mean)<(best["score_ratio"],best["mean_ratio"]):
            best=record
    chosen=[current["transforms"][f"cluster_{i}"] for i in range(12)]+best["top_row"]
    proposal=plan_for(chosen,rationale=
        "Target the measured upper-edge hotspot; change only clusters 12–15, minimizing a locally calibrated static score across all clusters and six scenarios. Full co-simulation required.")
    output.mkdir(parents=True,exist_ok=False)
    write_json(destination,proposal)
    write_json(output/"search.json",dict(method="Complete 8^4 enumeration with lower three rows fixed",
        measurement=str(local(measurement).relative_to(ROOT)),starting_layout=current,
        loads=str(load_folder.relative_to(ROOT)),scales_um=scales.tolist(),scenarios=loads["scenarios"],
        measured_cluster_peak_rise_k=rise.tolist(),baseline_static_peaks=initial_peaks.tolist(),
        score_units="relative calibrated static proposal score; not a simulated temperature",
        limitation="One-layout calibration is a local heuristic, not validated predictive accuracy.",
        best=best,candidates=records))
    print(json.dumps(dict(best=best,layout=str(local(destination))),indent=2),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loads",type=Path,required=True)
    parser.add_argument("--measurement",type=Path,required=True)
    parser.add_argument("--layout",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--candidate",type=Path,required=True)
    args=parser.parse_args()
    refine(args.loads,args.measurement,args.layout,args.output,args.candidate)


if __name__=="__main__":
    main()
