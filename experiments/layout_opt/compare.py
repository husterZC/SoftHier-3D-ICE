#!/usr/bin/env python3
"""Check workload equality and render comparisons of complete layout trials."""

import argparse
import copy
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import PatchCollection, PolyCollection
from matplotlib.patches import Rectangle, Patch
from PIL import Image
from experiments.layout_opt.measure import analysis_module, rectangles
from experiments.layout_opt.native_export import native_contract
from experiments.layout_opt.transforms import local, write_json
from experiments.llm_dram3d.opt_steps import opt_label, opt_metadata

PROFILES = ("constant", "temperature_aware")
COLORS = ("#333333", "#0072B2", "#D55E00", "#009E73", "#CC79A7")
LABELS = {"00_baseline": "Default", "01_edge_inward_v2": "Edge inward",
          "02_balanced_search": "Balanced", "03_top_refinement": "Top-row refinement"}
KINDS = dict(light_redmule="#D55E00", memory="#56B4E9", spatz="#009E73",
             core="#0072B2", idma="#CC79A7", floonoc="#E69F00", transpose="#777777")


def read_json(path):
    return json.loads(local(path).read_text())


def label(item):
    return LABELS.get(item["name"], item["name"])


def contract(item, profile="temperature_aware"):
    return read_json(ROOT / item["profiles"][profile]["run"] / "generated/system_config.json")


def validate_comparison(items, folders):
    """Reject physical, input, timing or activity changes that confound placement."""
    reference = items[0]
    if set(reference["layout"]["transforms"].values()) != {"R0"}:
        raise ValueError("first trial must be the untransformed baseline")
    studies = [read_json(ROOT / item["trial"] / "study.json") for item in items]
    settings = ("cases", "layer", "seed", "repeats", "all_experts", "opt_step",
        "interval_ps", "thermal_step_ps", "target_cells", "estimate_scale",
        "floorplan_rule", "thermal_replay", "thermal_replay_till", "allow_model_clamp",
        "dram_power_temperature_dependence", "sdk_commit", "arch_sha256",
        "source_sha256", "native_source_sha256", "thermal_config_sha256",
        "dram_power_config_sha256", "resolved_thermal_config", "resolved_dram_power_config")
    for study in studies:
        if any(study[key] != studies[0][key] for key in settings):
            raise ValueError("trial physical or workload settings differ")
    evidence = []
    for item, folder in zip(items, folders):
        deltas = {}
        for profile in PROFILES:
            a, b = reference["profiles"][profile], item["profiles"][profile]
            for field in ("binary_sha256", "preload_sha256", "stage_cycles", "layer_time_us", "slots"):
                if a[field] != b[field]:
                    raise ValueError(f"{item['name']}/{profile}: {field} differs")
            base, candidate = contract(reference, profile), contract(item, profile)
            untransformed = copy.deepcopy(base)
            untransformed["metadata"].pop("layout_optimization")
            expected = native_contract(untransformed, item["layout"])
            # Layout hashing records the source JSON bytes; geometry is checked
            # independently instead of assuming matching serialized plan hashes.
            if candidate["geometry"] != expected["geometry"]:
                raise ValueError("candidate is not the requested rigid cluster transform")
            for key in base.keys() | candidate.keys():
                if key not in ("geometry", "metadata") and base.get(key) != candidate.get(key):
                    raise ValueError("non-geometric contract change: " + key)
            metadata = [dict(c["metadata"]) for c in (base, candidate)]
            for value in metadata:
                value.pop("layout_optimization")
            if metadata[0] != metadata[1]:
                raise ValueError("non-layout metadata differs")
            with np.load(local(folders[0] / f"{profile}_activity.npz")) as left, \
                 np.load(local(folder / f"{profile}_activity.npz")) as right:
                for key in ("paths", "start_ps", "end_ps"):
                    if not np.array_equal(left[key], right[key]):
                        raise ValueError("activity indexing differs: " + key)
                for key in ("dynamic_w", "dram_background_w"):
                    if left[key].shape != right[key].shape or not np.allclose(
                            left[key], right[key], rtol=1e-6, atol=1e-10):
                        raise ValueError("activity differs: " + key)
                deltas[profile] = float(np.max(np.abs(left["dynamic_w"]-right["dynamic_w"])))
            for die in a["dies"]:
                if a["dies"][die]["cell_count"] != b["dies"][die]["cell_count"]:
                    raise ValueError("native mesh cell count differs")
        evidence.append(dict(name=item["name"], max_dynamic_delta_w=deltas,
            matching_inputs=True, matching_timing=True, legal_rigid_transforms=True))
    return evidence


def save(fig, output, name):
    fig.savefig(output / f"{name}.png", dpi=170, bbox_inches="tight")
    fig.savefig(output / f"{name}.pdf", bbox_inches="tight")
    plt.close(fig)


def layout_plot(items, output):
    fig, axes = plt.subplots(1, len(items), figsize=(4.1*len(items), 4.3), squeeze=False,
                             layout="constrained")
    for axis, item in zip(axes[0], items):
        config = contract(item)
        kinds = {c["power_column"]: c["kind"] for c in config["metadata"]["components"]}
        boxes = rectangles(config)
        patches, colors = [], []
        for name, (x,y,w,h) in boxes.items():
            patches.append(Rectangle((x/1000,y/1000),w/1000,h/1000))
            colors.append(KINDS.get(kinds.get(name), "#eeeeee"))
        axis.add_collection(PatchCollection(patches, facecolors=colors, edgecolors="white", linewidths=.2))
        for name, cluster in config["geometry"]["chip"]["subs"].items():
            x,y = np.asarray(cluster["offset"])/1000
            w,h = np.asarray(cluster["shape"])/1000
            axis.add_patch(Rectangle((x,y),w,h,facecolor="none",edgecolor="#333333",linewidth=.7))
            red = boxes[f"chip/{name}/redmule"]
            axis.text((red[0]+red[2]/2)/1000, (red[1]+red[3]/2)/1000,
                f"{name.split('_')[-1]}\n{item['layout']['transforms'][name]}",
                ha="center", va="center", color="white", fontsize=7)
        width,height = np.asarray(config["geometry"]["chip"]["shape"])/1000
        axis.set(xlim=(0,width), ylim=(0,height), aspect="equal", xlabel="x (mm)",
                 ylabel="y (mm)", title=label(item))
    fig.legend(handles=[Patch(color=KINDS[k], label=n) for k,n in
        (("light_redmule","RedMulE"),("memory","SRAM"),("spatz","Spatz"),("core","Cores"))],
        loc="outside lower center", ncol=4)
    save(fig, output, "layouts")


def curve_plot(items, folders, output):
    fig, axes = plt.subplots(2,2,figsize=(11,7), sharex=True, sharey="row", layout="constrained")
    for column, profile in enumerate(PROFILES):
        for item, folder, color in zip(items, folders, COLORS):
            with np.load(folder / f"{profile}_temperature.npz") as data:
                for row, die in enumerate(("LOGIC_DIE", "DRAM_DIE")):
                    axes[row,column].plot(data["end_ps"]*1e-9, data[die], label=label(item), color=color)
        start,end = np.array(items[0]["profiles"][profile]["moe_bounds_ps"])*1e-9
        for axis in axes[:,column]:
            axis.axvspan(start,end,color="#b0b0b0",alpha=.13,label="MoE phase")
            axis.grid(alpha=.2)
        axes[0,column].set_title(profile.replace("_"," "))
        axes[1,column].set_xlabel("Power-window end (ms)")
    axes[0,0].set_ylabel("Logic: peak cell (°C)")
    axes[1,0].set_ylabel("DRAM: peak cell (°C)")
    axes[0,0].legend(fontsize=8)
    study = read_json(ROOT / items[0]["trial"] / "study.json")
    cooling = study["resolved_thermal_config"]["heat_transfer_coefficient_w_m2_k"]
    fig.suptitle(f"{study['cases'][0]} / {opt_label(study['opt_step'])}\n"
                 f"Complete executions, no replay; h = {cooling:,.0f} W/(m² K)")
    save(fig, output, "temperature_history")


def redmule_plot(items, best, output):
    fig, axes = plt.subplots(1,2,figsize=(11,4),layout="constrained")
    x = np.arange(16)
    for item,color in zip(items,COLORS):
        components = item["profiles"]["temperature_aware"]["components"]
        axes[0].plot(x,[components[f"chip/cluster_{i}/redmule"]["peak_component_c"] for i in x],
                     "o-",ms=4,label=label(item),color=color)
    components = items[0]["profiles"]["constant"]["components"]
    axes[1].bar(x,[components[f"chip/cluster_{i}/redmule"]["moe_average_dynamic_w"] for i in x],color="#0072B2")
    axes[0].set(ylabel="RedMulE peak component average (°C)",title="Temperature-aware leakage")
    axes[1].set(ylabel="Mean MoE dynamic power (W)",title="Baseline activity; matched across layouts")
    for axis in axes:
        axis.set(xlabel="Cluster index",xticks=x)
        axis.grid(axis="y",alpha=.2)
    axes[0].legend(fontsize=8)
    save(fig, output, "redmule_temperature_and_work")


def thermal_maps(items, output, animations):
    common = analysis_module().load_common()
    profile = "temperature_aware"
    count = items[0]["profiles"][profile]["slots"]
    study = read_json(ROOT / items[0]["trial"] / "study.json")
    interval_ms = study["interval_ps"] * 1e-9
    columns = min(2, len(items))
    rows = (len(items) + columns - 1) // columns
    manifest = dict(profile=profile, panel_order=[item["name"] for item in items],
        panel_order_direction="left to right, then top to bottom", grid=[rows,columns],
        interval_ms=interval_ms, synchronized_animations=True, dies={})
    for die_id in ("LOGIC_DIE", "DRAM_DIE"):
        peaks = [item["profiles"][profile]["dies"][die_id] for item in items]
        indices = sorted(set(np.linspace(0,count-1,min(count,80),dtype=int).tolist() +
                             [int(p["peak_slot"]) for p in peaks]))
        data = []
        for item in items:
            folder = local(ROOT / item["profiles"][profile]["run"] / "results/3dice")
            native_manifest = read_json(folder / "thermal_manifest.json")
            die = next(d for d in native_manifest["dies"] if d["id"] == die_id)
            geometry = common.VIEW.load_tmap_geometry_cells(local(folder / die["coordinates_output"]))
            frames = common.VIEW.read_tmap_gif_frame_rows(local(folder / die["map_output"]),
                geometry["cell_count"],indices,np)
            data.append((geometry,frames))
        minimum = min(p["minimum_cell_c"] for p in peaks)
        maximum = max(p["peak_cell_c"] for p in peaks)
        fig,grid = plt.subplots(rows,columns,figsize=(4.8*columns,4.3*rows),
                                squeeze=False,layout="constrained")
        axes = list(grid.flat[:len(items)])
        for axis in grid.flat[len(items):]:
            axis.set_visible(False)
        collections, titles = [], []
        for axis,item,(geometry,frames) in zip(axes,items,data):
            collection = PolyCollection(common.VIEW.tmap_geometry_vertices(geometry,np)/1000,
                cmap="inferno",edgecolors="none",clim=(minimum,maximum))
            axis.add_collection(collection)
            bounds = geometry["bounds"]
            axis.set(xlim=(bounds["minX"]/1000,bounds["maxX"]/1000),
                     ylim=(bounds["minY"]/1000,bounds["maxY"]/1000),
                     xlabel="x (mm)",ylabel="y (mm)",aspect="equal")
            if die_id == "LOGIC_DIE":
                for name,(x,y,w,h) in rectangles(contract(item)).items():
                    if name.endswith("/redmule"):
                        axis.add_patch(Rectangle((x/1000,y/1000),w/1000,h/1000,
                            facecolor="none",edgecolor="#bdbdbd",linewidth=.35))
            collections.append(collection)
            titles.append(axis.set_title("",fontsize=11))
        fig.colorbar(collections[0],ax=axes,label="Native cell temperature (°C)",shrink=.85)
        subtitle = fig.suptitle("",fontsize=12)
        def update_panel(panel, frame_index, own_peak=False):
            item = items[panel]
            values = data[panel][1][frame_index]["values"]-273.15
            collections[panel].set_array(values)
            if own_peak:
                detail = f"Run peak: {values.max():.3f} °C at {(indices[frame_index]+1)*interval_ms:.2f} ms"
            else:
                detail = f"Current peak: {values.max():.3f} °C"
            titles[panel].set_text(f"{panel}. {label(item)}\n{detail}")
        def update(frame_index):
            for panel in range(len(items)):
                update_panel(panel,frame_index)
            slot = indices[frame_index]
            subtitle.set_text(f"{die_id}: temperature-aware, no replay — {(slot+1)*interval_ms:.2f} ms")
        update(len(indices)-1)
        fig.savefig(output / f"{die_id.lower()}_final.png",dpi=180)
        # Matched-time views keep every layout at the baseline's true peak slot.
        update(indices.index(peaks[0]["peak_slot"]))
        fig.savefig(output / f"{die_id.lower()}_baseline_peak.png",dpi=180)
        # This separate view deliberately uses each layout's own peak time.
        for panel,peak in enumerate(peaks):
            frame_index = indices.index(peak["peak_slot"])
            actual_peak = float(data[panel][1][frame_index]["values"].max())-273.15
            if not np.isclose(actual_peak,peak["peak_cell_c"],rtol=0,atol=1e-9):
                raise ValueError("rendered peak frame differs from measured peak")
            update_panel(panel,frame_index,own_peak=True)
        subtitle.set_text(f"{die_id}: each layout at its own peak — temperature-aware, no replay")
        fig.savefig(output / f"{die_id.lower()}_own_peaks.png",dpi=180)
        fig.savefig(output / f"{die_id.lower()}_own_peaks.pdf")
        if animations:
            frames = []
            for frame_index in range(len(indices)):
                update(frame_index)
                fig.canvas.draw()
                frames.append(Image.fromarray(np.asarray(fig.canvas.buffer_rgba()).copy()).convert("RGB"))
            durations = [max(40,int((b-a)*20)) for a,b in zip(indices,indices[1:])] + [700]
            frames[0].save(output / f"{die_id.lower()}_comparison.gif",save_all=True,
                append_images=frames[1:],duration=durations,loop=0)
        plt.close(fig)
        manifest["dies"][die_id] = dict(color_limits_c=[minimum,maximum],
            frame_slots=indices, frame_times_ms=[(slot+1)*interval_ms for slot in indices],
            baseline_peak_slot=peaks[0]["peak_slot"], final_slot=count-1,
            layout_peaks=[dict(name=item["name"],slot=peak["peak_slot"],
                time_ms=(peak["peak_slot"]+1)*interval_ms,peak_cell_c=peak["peak_cell_c"])
                for item,peak in zip(items,peaks)])
    write_json(output / "thermal_maps.json",manifest)


def compare(folders, output, animations=False):
    folders = [local(path) for path in folders]
    output = local(output)
    items = [read_json(path / "metrics.json") for path in folders]
    evidence = validate_comparison(items,folders)
    output.mkdir(parents=True,exist_ok=True)
    best = min(items,key=lambda i:i["score_c"])
    baseline = items[0]["score_c"]
    reduction = baseline-best["score_c"]
    rows = []
    for item in items:
        for profile in PROFILES:
            m = item["profiles"][profile]
            rows.append(dict(name=item["name"],profile=profile,peak_cell_c=m["peak_cell_c"],
                peak_logic_cell_c=m["dies"]["LOGIC_DIE"]["peak_cell_c"],
                peak_dram_cell_c=m["dies"]["DRAM_DIE"]["peak_cell_c"],
                redmule_spread_c=m["redmule_peak_spread_c"],layer_time_us=m["layer_time_us"],
                dynamic_energy_j=m["dynamic_energy_j"],leakage_energy_j=m["leakage_energy_j"]))
    summary = dict(objective=items[0]["objective"],best_name=best["name"],
        baseline_peak_c=baseline,best_peak_c=best["score_c"],reduction_c=reduction,
        reduction_of_rise_above_ambient_pct=100*reduction/(baseline-26.85),
        ambient_c=26.85,validation=evidence,results=rows,
        limitation="Best among the fully evaluated layouts; no global optimum claim.")
    study = read_json(ROOT / items[0]["trial"] / "study.json")
    summary.update(case=study["cases"][0], **opt_metadata(study["opt_step"]),
        heat_transfer_coefficient_w_m2_k=study["resolved_thermal_config"]["heat_transfer_coefficient_w_m2_k"])
    write_json(output / "comparison.json",summary)
    with (output / "comparison.csv").open("w") as stream:
        writer = csv.DictWriter(stream,fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    layout_plot(items,output)
    curve_plot(items,folders,output)
    redmule_plot(items,best,output)
    thermal_maps(items,output,animations)
    print(json.dumps({k:v for k,v in summary.items() if k not in ("results","validation")},indent=2),flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results",type=Path,nargs="+",required=True,
                        help="completed measurement directories, baseline first")
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--animations",action="store_true")
    args = parser.parse_args()
    compare(args.results,args.output,args.animations)


if __name__ == "__main__":
    main()
