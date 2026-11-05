#!/usr/bin/env python3
"""Compare all four completed one-second layout replays on shared scales."""

import argparse
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
from matplotlib.collections import PolyCollection
from matplotlib.patches import Rectangle
from PIL import Image
from experiments.layout_opt.compare import COLORS, LABELS, layout_plot, save
from experiments.layout_opt.measure import rectangles
from experiments.layout_opt.replay_study import CAMPAIGN, HERE, NAMES, check_complete
from experiments.layout_opt.transforms import local, write_json
from experiments.thermal_replay.activity import digest, load_recording
from experiments.thermal_replay.analyze import VIEW
from experiments.llm_dram3d.opt_steps import opt_label, opt_metadata

PROFILES = ("constant", "temperature_aware")
DIES = ("LOGIC_DIE", "DRAM_DIE")


def read(path):
    return json.loads(local(path).read_text())


def measurements(campaign):
    folder = local(HERE / "artifacts" / campaign)
    output = local(HERE / "results" / campaign)
    study = read(folder / "study.json")
    entries = study["layouts"]
    if [e["name"] for e in entries] != list(NAMES):
        raise ValueError("unexpected layouts/order")
    data, reference = {}, None
    for entry in entries:
        name = entry["name"]
        manifest, document, activity = load_recording(local(ROOT / entry["recording"]))
        if manifest["sha256"] != entry["recording_sha256"]:
            raise ValueError("recording changed since preparation")
        if document["metadata"]["layout_optimization"]["transforms"] != entry["layout"]["transforms"]:
            raise ValueError("recording layout differs")
        data[name] = {}
        pair = []
        for profile in PROFILES:
            path = folder / "runs" / name / profile
            summary = check_complete(path,study,entry,profile)
            metrics = read(output / name / profile / "metrics.json")
            if metrics["summary_sha256"] != digest(path / "summary.json"):
                raise ValueError("measurement summary differs from the run")
            common = (metrics["dynamic_sha256"],metrics["dram_background_sha256"],
                summary["slot_ps"],summary["step_ps"],summary["period_ps"],
                summary["layer_start_ps"],summary["slots"],summary["simulated_seconds"],
                summary["server_sha256"],metrics["initial_temperature_c"])
            if reference is None:
                reference = common
            elif reference != common:
                raise ValueError("replayed activity, timing, solver or initial state differs")
            data[name][profile] = metrics
            pair.append(path)
        for filename in ("ice.stk", "thermal_manifest.json", "components.json"):
            if (pair[0]/filename).read_bytes() != (pair[1]/filename).read_bytes():
                raise ValueError("paired physical settings or mappings differ")
    return study,data


def history_plot(campaign,data,output,study):
    fig,axes = plt.subplots(2,2,figsize=(11,7),sharex=True,sharey="row",layout="constrained")
    for column,profile in enumerate(PROFILES):
        for name,color in zip(NAMES,COLORS):
            for row,die in enumerate(DIES):
                with np.load(HERE / "results" / campaign / name / profile / f"{die.lower()}.npz") as native:
                    axes[row,column].plot(native["time_ps"]*1e-12,native["peak_c"],
                                          label=LABELS[name],color=color,lw=.8)
        axes[0,column].set_title(profile.replace("_"," "))
        axes[1,column].set_xlabel("Simulated time (s)")
        for axis in axes[:,column]:
            axis.grid(alpha=.2)
    axes[0,0].set_ylabel("Logic: peak native cell (°C)")
    axes[1,0].set_ylabel("DRAM: peak native cell (°C)")
    axes[0,0].legend(fontsize=8)
    fig.suptitle(f"{study['case']} / {opt_label(study['opt_step'])}\n"
        f"1 s replay, h = {study['heat_transfer_coefficient_w_m2_k']:,.0f}, model clamp enabled")
    save(fig,output,"temperature_history")


def peak_plot(data,output):
    fig,axes = plt.subplots(1,2,figsize=(11,4.2),layout="constrained")
    for axis,profile in zip(axes,PROFILES):
        values = [data[name][profile]["peak_cell_c"] for name in NAMES]
        bars = axis.bar(np.arange(4),np.array(values)-26.85,bottom=26.85,color=COLORS[:4])
        axis.bar_label(bars,labels=[f"{value:.3f} °C" for value in values],padding=4,fontsize=9)
        axis.set(xticks=np.arange(4),xticklabels=["Default","Edge inward","Balanced","Top-row"],
                 title=profile.replace("_"," "),ylabel="Peak native cell (°C)",
                 ylim=(26.85,max(values)+(max(values)-26.85)*.12))
        axis.grid(axis="y",alpha=.2)
    fig.suptitle("Peak over both active dies and all 50,000 thermal slots")
    save(fig,output,"peak_temperatures")


def read_frame(path,offset,count):
    with local(path).open("rb") as stream:
        stream.seek(int(offset))
        values = np.fromstring(stream.readline(),sep=" ")-273.15
    if len(values) != count or not np.isfinite(values).all():
        raise ValueError("invalid indexed native frame")
    return values


def maps(campaign,study,data,output,animations):
    map_info = dict(panel_order=list(NAMES),grid=[2,2],profiles={})
    documents = {entry["name"]: read(ROOT / entry["recording"] / "system_config.json")
                 for entry in study["layouts"]}
    interval_s = study["interval_ps"]*1e-12
    for profile in PROFILES:
        map_info["profiles"][profile] = {}
        for die in DIES:
            metrics = [data[name][profile]["dies"][die] for name in NAMES]
            # Use the same color limits for both profiles as well as all layouts.
            limits = (min(data[name][p]["dies"][die]["minimum_cell_c"] for p in PROFILES for name in NAMES),
                      max(data[name][p]["dies"][die]["peak_cell_c"] for p in PROFILES for name in NAMES))
            slots = sorted(set([0,*range(499,50000,500),49999] + [m["peak_slot"] for m in metrics]))
            loaded = []
            for name,measurement in zip(NAMES,metrics):
                with np.load(HERE / "results" / campaign / name / profile / f"{die.lower()}.npz") as stored:
                    cells = stored["cells"].copy()
                    frames = {int(slot):values.copy() for slot,values in zip(stored["frame_slots"],stored["frames_c"])}
                    for slot in slots:
                        if slot not in frames:
                            frames[slot] = read_frame(ROOT / measurement["map_file"],
                                                     stored["row_offsets"][slot],len(cells))
                    for slot in slots:
                        if not np.isclose(frames[slot].max(),stored["peak_c"][slot],rtol=0,atol=1e-9):
                            raise ValueError("frame peak differs from complete native history")
                loaded.append((cells,frames))
            fig,axes = plt.subplots(2,2,figsize=(9.6,8.6),layout="constrained")
            collections,titles = [],[]
            for axis,name,(cells,frames) in zip(axes.flat,NAMES,loaded):
                geometry = dict(cells=cells)
                # Native coordinates round to 0.01 um. Face-colored hairline
                # borders prevent those subpixel gaps appearing as white seams.
                collection = PolyCollection(VIEW["tmap_geometry_vertices"](geometry,np)/1000,
                    cmap="inferno",edgecolors="face",linewidths=.1,antialiaseds=False,clim=limits)
                axis.add_collection(collection)
                axis.set(xlim=(cells[:,0].min()/1000,(cells[:,0]+cells[:,2]).max()/1000),
                         ylim=(cells[:,1].min()/1000,(cells[:,1]+cells[:,3]).max()/1000),
                         aspect="equal",xlabel="x (mm)",ylabel="y (mm)")
                if die == "LOGIC_DIE":
                    for component,(x,y,w,h) in rectangles(documents[name]).items():
                        if component.endswith("/redmule"):
                            axis.add_patch(Rectangle((x/1000,y/1000),w/1000,h/1000,
                                facecolor="none",edgecolor="#bdbdbd",linewidth=.35))
                collections.append(collection)
                titles.append(axis.set_title("",fontsize=11))
            fig.colorbar(collections[0],ax=list(axes.flat),label="Native cell temperature (°C)",shrink=.85)
            subtitle = fig.suptitle("",fontsize=11)
            def update(frame_slots,own=False):
                for panel,(name,slot,(_,frames)) in enumerate(zip(NAMES,frame_slots,loaded)):
                    values = frames[slot]
                    collections[panel].set_array(values)
                    detail = f"{'Run' if own else 'Current'} peak: {values.max():.3f} °C"
                    if own:
                        detail += f" at {(slot+1)*interval_s:.5f} s"
                    titles[panel].set_text(f"{panel}. {LABELS[name]}\n{detail}")
                timing = "each layout at its own peak" if own else f"{(frame_slots[0]+1)*interval_s:.5f} s"
                alias = opt_metadata(study["opt_step"])["opt_alias"]
                subtitle.set_text(f"{die}: {profile.replace('_',' ')} — {timing}\n"
                    f"{study['case']}; OPT {study['opt_step']}" + (f" ({alias})" if alias else "") +
                    f"; h = {study['heat_transfer_coefficient_w_m2_k']:,.0f}; model clamp enabled")
            stem = f"{die.lower()}_{profile}"
            update([49999]*4)
            fig.savefig(output / f"{stem}_final.png",dpi=180)
            fig.savefig(output / f"{stem}_final.pdf")
            update([m["peak_slot"] for m in metrics],own=True)
            fig.savefig(output / f"{stem}_own_peaks.png",dpi=180)
            fig.savefig(output / f"{stem}_own_peaks.pdf")
            # Preserve the full-history scale above, and provide a second view
            # that resolves spatial differences after the whole die has warmed.
            # All four panels still use exactly the same temperature limits.
            detail_limits = (min(frames[m["peak_slot"]].min()
                for (_,frames),m in zip(loaded,metrics)),
                max(m["peak_cell_c"] for m in metrics))
            for collection in collections:
                collection.set_clim(*detail_limits)
            subtitle.set_text(subtitle.get_text()+" — detail scale")
            fig.savefig(output / f"{stem}_own_peaks_detail.png",dpi=180)
            fig.savefig(output / f"{stem}_own_peaks_detail.pdf")
            for collection in collections:
                collection.set_clim(*limits)
            if animations:
                rendered = []
                for slot in slots:
                    update([slot]*4)
                    fig.canvas.draw()
                    rendered.append(Image.fromarray(np.asarray(fig.canvas.buffer_rgba()).copy()).convert("RGB"))
                durations = [max(20,round((b-a)*interval_s*10000)) for a,b in zip(slots,slots[1:])] + [900]
                rendered[0].save(output / f"{stem}_comparison.gif",save_all=True,
                    append_images=rendered[1:],duration=durations,loop=0)
            plt.close(fig)
            map_info["profiles"][profile][die] = dict(color_limits_c=list(limits),
                peak_detail_color_limits_c=[float(value) for value in detail_limits],
                frame_slots=slots,frame_times_s=[(slot+1)*interval_s for slot in slots],
                layout_peak_slots=[m["peak_slot"] for m in metrics],synchronized=True)
            print(f"Rendered {die}/{profile}: all four layouts",flush=True)
    write_json(output / "thermal_maps.json",map_info)


def compare(campaign,animations=True):
    study,data = measurements(campaign)
    output = local(HERE / "results" / campaign / "final_comparison")
    output.mkdir(parents=True,exist_ok=True)
    rows = []
    for name in NAMES:
        for profile in PROFILES:
            m = data[name][profile]
            rows.append(dict(name=name,profile=profile,peak_cell_c=m["peak_cell_c"],
                peak_logic_cell_c=m["dies"]["LOGIC_DIE"]["peak_cell_c"],
                peak_dram_cell_c=m["dies"]["DRAM_DIE"]["peak_cell_c"],
                peak_component_c=m["component_metrics"]["peak_component_c"],
                redmule_peak_spread_c=m["redmule_peak_spread_c"],
                first_model_limit_seconds=m["model_limit_first_seconds"],
                dynamic_energy_j=m["component_metrics"]["dynamic_energy_j"],
                leakage_energy_j=m["component_metrics"]["leakage_energy_j"]))
    best = min(NAMES,key=lambda name:data[name]["temperature_aware"]["peak_cell_c"])
    baseline = data[NAMES[0]]["temperature_aware"]["peak_cell_c"]
    summary = dict(case=study["case"],**opt_metadata(study["opt_step"]),
        heat_transfer_coefficient_w_m2_k=study["heat_transfer_coefficient_w_m2_k"],
        thermal_replay_till=1.,allow_model_clamp=True,
        objective="maximum native thermal-cell temperature across both dies and all slots; temperature_aware",
        all_eight_runs_completed=True,slots_per_run=50000,
        identical_replayed_dynamic_activity=True,identical_dram_background=True,
        best_name=best,baseline_peak_c=baseline,best_peak_c=data[best]["temperature_aware"]["peak_cell_c"],
        results=rows)
    summary["reduction_c"] = baseline-summary["best_peak_c"]
    write_json(output / "comparison.json",summary)
    with (output / "comparison.csv").open("w") as stream:
        writer = csv.DictWriter(stream,fieldnames=list(rows[0]))
        writer.writeheader();writer.writerows(rows)
    source_items = [read(ROOT / entry["measurement"] if "measurement" in entry
        else HERE / "results" / entry["name"] / "metrics.json") for entry in study["layouts"]]
    layout_plot(source_items,output)
    history_plot(campaign,data,output,study)
    peak_plot(data,output)
    maps(campaign,study,data,output,animations)
    write_json(output / "analysis_sources.json",{
        str(Path(__file__).relative_to(ROOT)):digest(__file__),
        "experiments/layout_opt/measure_replay.py":digest(HERE / "measure_replay.py")})
    print(json.dumps(summary,indent=2),flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign",default=CAMPAIGN)
    parser.add_argument("--animations",action="store_true")
    args = parser.parse_args()
    compare(args.campaign,args.animations)


if __name__ == "__main__":
    main()
