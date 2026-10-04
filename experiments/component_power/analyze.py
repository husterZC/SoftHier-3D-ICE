#!/usr/bin/env python3
"""Validate paired raw runs and write reports, CSVs, static plots and optional GIFs.

Reads study.json from run_study.py; does not run a new co-simulation. The raw
run directories and matching power-model tables must still be available.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from functools import lru_cache
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


# Reuse checked trace loading, exact-window integration, styling and rendering.
BASE = module("leakage_study_common", ROOT / "experiments/leakage_temperature/analyze.py")
VIEW = module("component_temperature_view", ROOT / "Interface_scripts/plot_runtime_temperature_map/plot_runtime_tmap.py")
MODELS = BASE.load_component_model_module()
np, plt = BASE.np, BASE.plt
from matplotlib.patches import Rectangle
from matplotlib.collections import PatchCollection

KINDS = ["core", "spatz", "light_redmule", "memory", "idma", "floonoc", "transpose"]
LABELS = dict(zip(KINDS, ["Scalar cores", "Spatz", "RedMulE", "SRAM", "iDMA", "NoC + NI", "Transpose"]))
COLORS = dict(zip(KINDS, ["#0072B2", "#009E73", "#D55E00", "#56B4E9", "#CC79A7", "#E69F00", "#777777"]))
NAMES = {"norm": "RMSNorm", "acti": "SiLU", "gemm": "SUMMA GEMM", "attn": "FlatAttention"}


def inventory(run):
    return {item["simulator_path"]: item for item in run.metadata["components"]}


def validate_inventory(run):
    paths = inventory(run)
    if len(paths) != len(run.metadata["components"]):
        raise ValueError("duplicate power domain in component inventory")
    for path in paths:
        if any(other.startswith(path + "/") for other in paths):
            raise ValueError(f"overlapping parent/child power domains: {path}")
    for record in run.power_records:
        actual = [item["path"] for item in record["components"]]
        if len(actual) != len(paths) or set(actual) != set(paths):
            raise ValueError("power trace does not cover the component inventory exactly once")
        for item in record["components"]:
            if any(not math.isfinite(item[field]) or item[field] < 0
                   for field in ("dynamic_w", "leakage_w", "total_w")):
                raise ValueError(f"invalid component power: {item['path']}")


def grouped_energy(run):
    info = inventory(run)
    energy = {kind: {"dynamic_j": 0., "leakage_j": 0.} for kind in KINDS}
    for record in run.power_records:
        dt = (record["window"]["end_ps"] - record["window"]["start_ps"]) * 1e-12
        for item in record["components"]:
            group = energy[info[item["path"]]["kind"]]
            for field in ("dynamic", "leakage"):
                group[field + "_j"] += item[field + "_w"] * dt
    return energy


def logic_clock_energy(run):
    energy = {kind: 0. for kind in KINDS if kind not in ("memory", "light_redmule")}
    voltage = run.metadata["power_voltage_v"]
    for item in inventory(run).values():
        if item["kind"] not in energy:
            continue
        sources = MODELS.logic_power_sources(item["kind"], tech_node=run.metadata["technology_node"],
            profile=run.profile, estimate_scale=run.metadata["power_estimate_scale"], **item["parameters"])
        table = sources["background"]["dynamic"]["values"]["25"]
        power = next(value["any"] for key, value in table.items() if float(key) == voltage)
        energy[item["kind"]] += power * run.duration_s
    return energy


@lru_cache(maxsize=32)
def cell_statistics(run_path):
    folder = Path(run_path) / "results/3dice"
    geometry = VIEW.load_tmap_geometry_cells(folder / "xyaxis_TOP_DIE.txt")
    count, minimum, maximum = VIEW.read_tmap_gif_stats(folder / "output_top_die.txt", geometry["cell_count"], np)
    return count, minimum, maximum


def metrics(run):
    energy = grouped_energy(run)
    dynamic = sum(v["dynamic_j"] for v in energy.values())
    leakage = sum(v["leakage_j"] for v in energy.values())
    first = sum(c["leakage_w"] for c in run.power_records[0]["components"])
    last = sum(c["leakage_w"] for c in run.power_records[-1]["components"])
    return {"duration_us": run.duration_ps * 1e-6,
            "peak_component_temperature_c": max(row["temperature_c"] for row in run.temperatures),
            "peak_cell_temperature_c": cell_statistics(str(run.path))[2] - 273.15,
            "average_dynamic_w": dynamic / run.duration_s,
            "average_leakage_w": leakage / run.duration_s,
            "dynamic_energy_uj": dynamic * 1e6, "leakage_energy_uj": leakage * 1e6,
            "total_energy_uj": (dynamic + leakage) * 1e6,
            "first_leakage_w": first, "last_leakage_w": last,
            "leakage_growth_during_run_pct": 100 * (last / first - 1)}


def validate_pair(constant, aware):
    for run in (constant, aware):
        validate_inventory(run)
    if constant.profile != "constant" or aware.profile != "temperature_aware":
        raise ValueError("profiles do not form a constant/temperature_aware pair")
    if constant.duration_ps != aware.duration_ps:
        raise ValueError("power feedback changed simulated execution time")
    contracts = [json.loads((r.path / "generated/system_config.json").read_text()) for r in (constant, aware)]
    for key in ("geometry", "floorplan", "thermal_feedback", "power_trace"):
        if contracts[0][key] != contracts[1][key]:
            raise ValueError(f"A/B {key} mismatch")
    for key in ("power_model_specs", "components", "power_voltage_v", "power_estimate_scale"):
        if constant.metadata[key] != aware.metadata[key]:
            raise ValueError(f"A/B model mismatch: {key}")
    for name in ("softhier.elf", "preload.elf"):
        digests = [hashlib.sha256((r.path / "artifacts" / name).read_bytes()).hexdigest() for r in (constant, aware)]
        if digests[0] != digests[1]:
            raise ValueError(f"A/B artifact mismatch: {name}")
    if len(constant.power_records) != len(aware.power_records):
        raise ValueError("A/B window-count mismatch")
    maximum_dynamic_difference = 0.
    for left, right in zip(constant.power_records, aware.power_records):
        if left["window"] != right["window"]:
            raise ValueError("A/B integration-window mismatch")
        for a, b in zip(left["components"], right["components"]):
            if a["path"] != b["path"] or not math.isclose(a["dynamic_w"], b["dynamic_w"], rel_tol=1e-6, abs_tol=1e-10):
                raise ValueError(f"A/B dynamic power mismatch: {a['path']}")
            maximum_dynamic_difference = max(maximum_dynamic_difference, abs(a["dynamic_w"] - b["dynamic_w"]))
    reference = {c["path"]: c["leakage_w"] for c in constant.power_records[0]["components"]}
    for record in constant.power_records:
        for item in record["components"]:
            if not math.isclose(item["leakage_w"], reference[item["path"]], rel_tol=1e-6, abs_tol=1e-10):
                raise ValueError(f"constant leakage changed: {item['path']}")
    return maximum_dynamic_difference


def validate_new_logic_leakage(run):
    """Check live temperature updates against the coefficient tables, not just monotonicity."""
    for name, archived in run.metadata["power_model_specs"].items():
        if archived != MODELS.model_spec(name):
            raise ValueError(f"current {name} model differs from the run's archived coefficients")
    tables = {}
    for path, item in inventory(run).items():
        if item["kind"] in ("memory", "light_redmule"):
            continue
        source = MODELS.logic_power_sources(item["kind"], tech_node=run.metadata["technology_node"],
            profile=run.profile, estimate_scale=run.metadata["power_estimate_scale"], **item["parameters"])
        values = source["background"]["leakage"]["values"]
        temps = sorted(float(t) for t in values)
        volts = run.metadata["power_voltage_v"]
        samples = [next(v["any"] for k, v in values[format(t, "g")].items() if float(k) == volts) for t in temps]
        tables[path] = (temps, samples)
    maximum_error = 0.
    for record in run.power_records:
        for item in record["components"]:
            if item["path"] not in tables:
                continue
            expected = np.interp(item["temperature_c"], *tables[item["path"]])
            error = abs(item["leakage_w"] - expected)
            maximum_error = max(maximum_error, error)
            if not math.isclose(item["leakage_w"], expected, rel_tol=1e-5, abs_tol=1e-9):
                raise ValueError(f"temperature-dependent leakage mismatch at {item['path']}: {item['leakage_w']} vs {expected}")
    return maximum_error


def csv_file(path, rows):
    if not rows:
        return
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def plot_pair(kernel, constant, aware, out):
    fig, axes = plt.subplots(1, 3, figsize=(13.2, 3.35), layout="constrained")
    for run, color, label in ((constant, BASE.CONSTANT_COLOR, "Constant leakage"), (aware, BASE.TEMPERATURE_COLOR, "Temperature-aware")):
        t, temperature = BASE.temperature_series(run)
        axes[0].plot(t * 1000, temperature, color=color, label=label)
        for axis, field in zip(axes[1:], ("total_w", "leakage_w")):
            t, power = BASE.power_series(run, field)
            axis.step(np.r_[0, t * 1000], np.r_[power[0], power], where="pre", color=color, label=label)
    for axis, label in zip(axes, ("Peak component temperature (°C)", "Total modeled power (W)", "Total leakage power (W)")):
        axis.set(xlabel="Simulation time (µs)", ylabel=label)
        axis.ticklabel_format(axis="y", useOffset=False)
    axes[0].legend(loc="best")
    fig.suptitle(f"{NAMES[kernel]} — identical workload, different leakage-temperature model")
    BASE.save_figure(fig, out, f"{kernel}_comparison")

    # Exactly two physical quantities versus time, with explicitly separate units.
    fig, axes = plt.subplots(2, 3, figsize=(13.2, 6.6), layout="constrained")
    info = inventory(aware)
    for kind, axis in zip(KINDS[:6], axes.flat):
        candidates = [row for row in aware.temperatures if info[row["path"]]["kind"] == kind]
        path = max(candidates, key=lambda row: row["temperature_c"])["path"]
        right = axis.twinx()
        t, leakage = BASE.power_series(aware, "leakage_w", path)
        axis.step(np.r_[0, t * 1000], np.r_[leakage[0], leakage] * 1000, where="pre", color=BASE.CONSTANT_COLOR, label="Leakage")
        t, temperature = BASE.temperature_series(aware, path)
        right.plot(t * 1000, temperature, color=BASE.TEMPERATURE_COLOR, label="Temperature")
        axis.set(xlabel="Simulation time (µs)", ylabel="Leakage (mW)", title=LABELS[kind])
        right.set_ylabel("Temperature (°C)", color=BASE.TEMPERATURE_COLOR)
        axis.tick_params(axis="y", labelcolor=BASE.CONSTANT_COLOR)
        right.tick_params(axis="y", labelcolor=BASE.TEMPERATURE_COLOR)
        right.grid(False)
        for a in (axis, right):
            a.ticklabel_format(axis="y", useOffset=False)
        axis.text(.03, .96, path.replace("/chip/", ""), transform=axis.transAxes, va="top", fontsize=7)
    fig.suptitle(f"{NAMES[kernel]}: temperature-aware leakage and temperature versus time\nHottest instance of each type; power is the interval average, temperature is the returned endpoint")
    BASE.save_figure(fig, out, f"{kernel}_leakage_temperature_time")


def plot_summary(pairs, out):
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4), layout="constrained")
    x = np.arange(len(pairs))
    bottom = np.zeros(len(pairs))
    for kind in KINDS:
        values = np.array([grouped_energy(a)[kind]["dynamic_j"] / a.duration_s for c, a in pairs.values()])
        axes[0].bar(x, values, bottom=bottom, label=LABELS[kind], color=COLORS[kind])
        bottom += values
    axes[0].set(ylabel="Mean dynamic power (W)", title="Activity + residual clocks by component")
    axes[0].legend(ncol=2, fontsize=8)
    leak, total = [], []
    for constant, aware in pairs.values():
        c, a = metrics(constant), metrics(aware)
        leak.append(100 * (a["leakage_energy_uj"] / c["leakage_energy_uj"] - 1))
        total.append(100 * (a["total_energy_uj"] / c["total_energy_uj"] - 1))
    axes[1].bar(x - .18, leak, .36, label="Leakage energy", color=BASE.TEMPERATURE_COLOR)
    axes[1].bar(x + .18, total, .36, label="Total modeled energy", color=BASE.CONSTANT_COLOR)
    axes[1].set(ylabel="Increase over constant leakage (%)", title="Temperature-aware energy impact")
    axes[1].legend()
    for axis in axes:
        axis.set_xticks(x, [NAMES[k] for k in pairs])
    BASE.save_figure(fig, out, "workload_summary")


def plot_assumptions(out):
    fig, axes = plt.subplots(1, 2, figsize=(10.5, 3.6), layout="constrained")
    energies = []
    for node in ("22nm", "12nm", "7nm", "5nm"):
        sources = MODELS.logic_power_sources("spatz", tech_node=node, profile="temperature_aware",
            function_units=4, vrf_bytes=1024, vlsu_ports=8)
        voltage = MODELS.technology_spec(node)["nominal_voltage_v"]
        values = sources["background"]["leakage"]["values"]
        temperatures = sorted(float(t) for t in values)
        leakage = np.array([next(v["any"] for k, v in values[format(t, "g")].items() if float(k) == voltage) for t in temperatures])
        axes[0].plot(temperatures, leakage / leakage[0], "o-", ms=3, label=node)
        energies.append([next(v["any"] for k, v in sources[f"fma_{precision}"]["dynamic"]["values"]["25"].items() if float(k) == voltage) for precision in (16, 64)])
    axes[0].set(xlabel="Temperature (°C)", ylabel="Logic leakage / leakage at 25 °C", title="Assumed sampled exponential leakage")
    axes[0].legend()
    energies = np.array(energies)
    x = np.arange(4)
    axes[1].bar(x - .18, energies[:, 0], .36, color=COLORS["spatz"], label="FP16 estimate")
    axes[1].bar(x + .18, energies[:, 1], .36, color=COLORS["core"], label="FP64 projection")
    axes[1].set_xticks(x, ["22 nm\n1.0 V", "12 nm\n0.9 V", "7 nm\n0.8 V", "5 nm\n0.7 V"])
    axes[1].set(ylabel="Arithmetic energy (pJ / active element)", title="FMA at nominal node voltage; VRF excluded")
    axes[1].legend()
    BASE.save_figure(fig, out, "model_assumptions")


def plot_floorplan(run, out):
    contract = json.loads((run.path / "generated/system_config.json").read_text())
    cluster = contract["geometry"]["chip"]["subs"]["cluster_0"]
    kind_by_name = {item["name"]: item["kind"] for item in inventory(run).values()}
    fig, (axis, legend) = plt.subplots(1, 2, figsize=(9, 7),
        gridspec_kw={"width_ratios": [1.6, 1]}, layout="constrained")
    callouts = []
    short_names = {"redmule": "RedMulE", "tcdm": "TCDM", "instr_mem": "Instr.\nSRAM",
        "stack_mem": "Stack\nSRAM", "data_noc_router": "Data\nrouter", "data_noc_ni": "NI", "idma": "iDMA"}
    for name, region in cluster["subs"].items():
        x, y = np.array(region["offset"]) / 1000
        w, h = np.array(region["shape"]) / 1000
        axis.add_patch(Rectangle((x, y), w, h, facecolor=COLORS.get(kind_by_name.get(name), "#eeeeee"), edgecolor="white", alpha=.8))
        rotate_label = w < .065 and h > .15
        label_y = y + h / 2
        if min(w, h) < .035 and not rotate_label:
            callouts.append(name)
            label = str(len(callouts))
            # Narrow neighboring columns in square_bands cannot hold adjacent
            # text on one baseline. Stagger their numbered callouts vertically.
            if w < .035 and h > .04:
                label_y = y + h * (.18 + .2 * ((len(callouts) - 1) % 4))
        else:
            label = short_names.get(name, name)
        axis.text(x + w/2, label_y, label, ha="center", va="center", fontsize=8,
                  rotation=90 if rotate_label else 0)
    axis.set(xlim=(0, cluster["shape"][0]/1000), ylim=(0, cluster["shape"][1]/1000),
             xlabel="x (mm)", ylabel="y (mm)")
    axis.set_aspect("equal")
    axis.grid(False)
    legend.axis("off")
    legend.text(0, .95, "Small regions", va="top", weight="bold", transform=legend.transAxes)
    for index, name in enumerate(callouts):
        legend.text(0, .9 - index * .045, f"{index+1}. {name.replace('_', ' ')}",
                    va="top", transform=legend.transAxes, fontsize=9)
    legend_top = min(.58, .85 - len(callouts) * .045)
    for index, kind in enumerate(KINDS):
        y = legend_top - index * .05
        legend.add_patch(Rectangle((0, y), .07, .025, transform=legend.transAxes, color=COLORS[kind]))
        legend.text(.1, y + .012, LABELS[kind], va="center", transform=legend.transAxes)
    count = run.metadata["cluster_grid"]["count"]
    legend.text(0, legend_top - .39, f"Area-estimated layout\nNot placement and routing\n\n{count} identical clusters", va="top", transform=legend.transAxes, fontsize=9)
    rule = run.metadata.get("floorplan_rule", "redmule_strip")
    fig.suptitle(f"Expanded cluster floorplan — {rule}")
    BASE.save_figure(fig, out, "cluster_floorplan")


def render_maps(kernel, constant, aware, out, animations, full_maps=False):
    data = []
    for run in (constant, aware):
        folder = run.path / "results/3dice"
        args = argparse.Namespace(gif_stride=1, vmin=None, vmax=None)
        data.append(VIEW.prepare_gif_data(args, folder / "floorplan_nopower.flp", folder / "output_top_die_flp_avg.txt", np))
    vmin = min(d["data_min"] for d in data)
    vmax = max(d["data_max"] for d in data)
    fig, axes = plt.subplots(1, 3, figsize=(12.8, 4), layout="constrained")
    for index, (axis, d) in enumerate(zip(axes[:2], data)):
        patches = [Rectangle((r["x"]/1000, r["y"]/1000), r["width"]/1000, r["height"]/1000) for r in d["regions"]]
        collection = PatchCollection(patches, cmap="inferno", edgecolors="none")
        collection.set_array(np.asarray(d["rows"][-1]["values"]) - 273.15)
        collection.set_clim(vmin - 273.15, vmax - 273.15)
        axis.add_collection(collection)
        axis.autoscale_view()
        axis.set_title(("Constant", "Temperature-aware")[index])
    fig.colorbar(collection, ax=list(axes[:2]), label="Component-average temperature (°C)", shrink=.85)
    difference = np.asarray(data[1]["rows"][-1]["values"]) - np.asarray(data[0]["rows"][-1]["values"])
    diff = PatchCollection(patches, cmap="magma", edgecolors="none")
    diff.set_array(difference)
    diff.set_clim(0, max(1e-6, float(difference.max())))
    axes[2].add_collection(diff)
    axes[2].autoscale_view()
    axes[2].set_title("Temperature-aware − constant")
    fig.colorbar(diff, ax=axes[2], label="Temperature difference (°C)", shrink=.85)
    for axis in axes:
        axis.set(xlabel="x (mm)", ylabel="y (mm)", aspect="equal")
        axis.grid(False)
    fig.suptitle(f"{NAMES[kernel]} — end-of-run floorplan temperatures")
    BASE.save_figure(fig, out, f"{kernel}_thermal_maps")
    if animations:
        for run, d in zip((constant, aware), data):
            folder = run.path / "results/3dice"
            command = [sys.executable, str(ROOT / "Interface_scripts/plot_runtime_temperature_map/plot_runtime_tmap.py"),
                "--floorplan", str(folder / "floorplan_nopower.flp"), "--tflp", str(folder / "output_top_die_flp_avg.txt"),
                "--gif", str(out / f"{kernel}_{run.profile}.gif"), "--gif-layout", "map", "--gif-width", "1000",
                "--gif-stride", str(max(1, len(d["rows"]) // 80)), "--gif-writer", "pillow",
                "--slot-seconds", str((run.power_records[0]["window"]["end_ps"] - run.power_records[0]["window"]["start_ps"]) * 1e-12),
                "--vmin", str(vmin), "--vmax", str(vmax), "--once", "--quiet"]
            subprocess.run(command, check=True)
        if full_maps:
            stats = [cell_statistics(str(run.path)) for run in (constant, aware)]
            lower = min(s[1] for s in stats)
            upper = max(s[2] for s in stats)
            for run, stat in zip((constant, aware), stats):
                folder = run.path / "results/3dice"
                subprocess.run([sys.executable, str(ROOT / "Interface_scripts/plot_runtime_temperature_map/plot_runtime_tmap.py"),
                    "--coords", str(folder / "xyaxis_TOP_DIE.txt"), "--map", str(folder / "output_top_die.txt"),
                    "--gif", str(out / f"{kernel}_{run.profile}_cells.gif"), "--gif-layout", "map", "--gif-width", "1000",
                    "--gif-stride", str(max(1, stat[0] // 80)), "--gif-writer", "pillow",
                    "--vmin", str(lower), "--vmax", str(upper), "--once", "--quiet"], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "Static PNG/PDF figures are always generated. Different floorplans, "
        "cell targets, repeat counts or estimate scales must be analyzed separately."))
    parser.add_argument("--manifest", type=Path, nargs="+", required=True,
        help="one or more study.json files with compatible settings and no duplicate kernel/profile runs")
    parser.add_argument("--output", type=Path, required=True,
        help="result directory; existing generated files with the same names are overwritten")
    parser.add_argument("--animations", action="store_true",
        help="also render component-average temperature GIFs for both profiles (default: off)")
    parser.add_argument("--full-maps", action="store_true",
        help="also render full thermal-cell GIFs with within-component gradients; requires --animations (default: off)")
    args = parser.parse_args()
    if args.full_maps and not args.animations:
        parser.error("--full-maps requires --animations")
    manifests = [json.loads(path.read_text()) for path in args.manifest]
    manifest = dict(manifests[0], runs=[])
    manifest["floorplan_rule"] = manifest.get("floorplan_rule", "redmule_strip")
    manifest["source_manifests"] = [str(path) for path in args.manifest]
    manifest["source_studies"] = [dict(path=str(path), prefix=entry["prefix"],
        provider_sha256=entry.get("provider_sha256")) for path, entry in zip(args.manifest, manifests)]
    if len(manifests) > 1:
        manifest["prefix"] = "combined"
        manifest.pop("provider_sha256", None)
    for entry in manifests:
        if entry.get("floorplan_rule", "redmule_strip") != manifest["floorplan_rule"]:
            raise ValueError("cannot combine different study settings: floorplan_rule")
        for key in ("target_cells", "estimate_scale", "repeats"):
            if entry.get(key, 1) != manifest.get(key, 1):
                raise ValueError(f"cannot combine different study settings: {key}")
        manifest["runs"].extend(entry["runs"])
    loaded = defaultdict(dict)
    for entry in manifest["runs"]:
        if entry["profile"] in loaded[entry["kernel"]]:
            raise ValueError(f"duplicate run in manifests: {entry['kernel']} {entry['profile']}")
        run = BASE.load_run(ROOT / entry["path"])
        if run.metadata.get("floorplan_rule", "redmule_strip") != manifest["floorplan_rule"]:
            raise ValueError("run floorplan rule differs from study manifest")
        loaded[entry["kernel"]][entry["profile"]] = run
    pairs = {k: (v["constant"], v["temperature_aware"]) for k, v in loaded.items()}
    args.output.mkdir(parents=True, exist_ok=True)
    BASE.configure_style()
    summary, components, series, checks = [], [], [], []
    for kernel, (constant, aware) in pairs.items():
        delta = validate_pair(constant, aware)
        for run in (constant, aware):
            error = validate_new_logic_leakage(run)
            checks.append(dict(kernel=kernel, profile=run.profile, max_dynamic_pair_difference_w=delta,
                               max_logic_leakage_model_error_w=error))
            summary.append(dict(kernel=kernel, profile=run.profile, **metrics(run)))
            clock_energy = logic_clock_energy(run)
            for kind, energy in grouped_energy(run).items():
                components.append(dict(kernel=kernel, profile=run.profile, component=kind,
                    dynamic_energy_uj=energy["dynamic_j"] * 1e6, leakage_energy_uj=energy["leakage_j"] * 1e6,
                    new_logic_clock_energy_uj=clock_energy[kind] * 1e6 if kind in clock_energy else "",
                    new_logic_event_energy_uj=max(0., energy["dynamic_j"] - clock_energy[kind]) * 1e6 if kind in clock_energy else ""))
                if kind in clock_energy and energy["dynamic_j"] < clock_energy[kind] - 1e-12:
                    raise ValueError(f"dynamic energy below configured residual clocks: {kernel} {kind}")
            info = inventory(run)
            temp_by_time = defaultdict(dict)
            for row in run.temperatures:
                temp_by_time[row["end_ps"]][row["path"]] = row["temperature_c"]
            for record in run.power_records:
                for kind in KINDS:
                    selected = [c for c in record["components"] if info[c["path"]]["kind"] == kind]
                    series.append(dict(kernel=kernel, profile=run.profile, component=kind,
                        start_us=record["window"]["start_ps"] * 1e-6, end_us=record["window"]["end_ps"] * 1e-6,
                        dynamic_w=sum(c["dynamic_w"] for c in selected), leakage_w=sum(c["leakage_w"] for c in selected),
                        peak_temperature_c=max(temp_by_time[record["window"]["end_ps"]][c["path"]] for c in selected)))
        plot_pair(kernel, constant, aware, args.output)
        render_maps(kernel, constant, aware, args.output, args.animations, args.full_maps)
    plot_summary(pairs, args.output)
    plot_floorplan(next(iter(pairs.values()))[1], args.output)
    plot_assumptions(args.output)
    csv_file(args.output / "metrics.csv", summary)
    csv_file(args.output / "component_energy.csv", components)
    csv_file(args.output / "timeseries.csv", series)
    csv_file(args.output / "validation.csv", checks)
    (args.output / "study.json").write_text(json.dumps(manifest, indent=2) + "\n")
    rows = []
    for kernel, (constant, aware) in pairs.items():
        c, a = metrics(constant), metrics(aware)
        rows.append(f"| {NAMES[kernel]} | {c['duration_us']:.3f} | {c['peak_component_temperature_c']:.4f} | {a['peak_component_temperature_c']:.4f} | {a['average_dynamic_w']:.4f} | {100*(a['leakage_energy_uj']/c['leakage_energy_uj']-1):.3f} | {100*(a['total_energy_uj']/c['total_energy_uj']-1):.3f} | {a['leakage_growth_during_run_pct']:.3f} |")
    report = """# Activity-driven component power and thermal feedback in SoftHier

## Abstract

This exploratory study extends the existing closed-loop GVSoC/3D-ICE framework
to scalar cores, Spatz, iDMA, NoC routers/interfaces, and a coarse transpose
model, in addition to RedMulE and SRAM. Matched SDK implementation workloads
isolate the effect of temperature-dependent leakage. Estimates are intended
for framework demonstrations and relative sensitivity, not silicon prediction.

## Method

The implementation architecture has 16 clusters, each with five scalar cores,
four Spatz units, a 32×16 RedMulE, 384 KiB TCDM, 64 KiB instruction memory,
128 KiB stack memory, one iDMA, a transpose engine, and data/synchronization
router-interface pairs. The 304 power domains map without hierarchical overlap
to an area-estimated floorplan with 320 regions. Nominal conditions are 5 nm,
0.7 V and 1 GHz. Technology projections and scalar/custom-operation estimates
are explicitly approximate. HBM/PHY and local uncharacterized support logic are
outside the reported power boundary. The existing thermal stack, mapping hook,
floorplan reader, and animation renderer are reused without solver changes.

Spatz FP64 operation/VRF reference values are adapted from
[Spatz, Figs. 9–12](https://arxiv.org/pdf/2309.10137v2). iDMA structural scaling
is guided by [iDMA, Section IV](https://arxiv.org/html/2305.05240v2); its per-byte
energy remains an estimate. Router energy uses the approximately 0.15 pJ/byte/hop
reference from [FlooNoC, Section VI-D](https://arxiv.org/pdf/2409.17606v2).
Collective reduction, low-precision and custom exponential coefficients are
not measured values from these papers.

Constant leakage holds the 25 °C references fixed. Temperature-aware leakage
uses the same references with an exponential law sampled at 25–125 °C and
linearly interpolated by GVSoC. At 5 nm, logic/SRAM doubling intervals are
22/27 °C. Initial temperature is 26.85 °C, so an initial reference offset exists
before self-heating. The final column below separately reports the additional
leakage growth during each temperature-aware run.

Each A/B pair uses byte-identical executable/preload artifacts, identical
geometry and time windows. Dynamic power is checked per component per window;
new logic leakage is independently checked against the configured table at
the temperature actually used. Energy integrates exact GVSoC interval lengths,
including the final partial interval. The thermal solver advances the final
partial interval as a full slot, an endpoint-temperature quantization error
bounded in time by one coupling interval. Component temperatures below are
floorplan averages, not maximum finite-volume cell temperatures.

## Results

| Kernel | Time (µs) | Peak T constant (°C) | Peak T aware (°C) | Mean dynamic (W) | Leakage energy Δ (%) | Total energy Δ (%) | Within-run leakage growth (%) |
|---|---:|---:|---:|---:|---:|---:|---:|
""" + "\n".join(rows) + """

The emitted `validation.csv` records the maximum A/B dynamic-power discrepancy
and leakage-table residual. Equal duration confirms that the power model does
not introduce temperature-dependent timing or throttling. Short cold-start
workloads need not produce a large thermal difference; an energy difference
caused by the initial reference offset is not evidence of substantial
self-heating or a steady-state thermal effect.

## Figures and reproducibility

- `workload_summary`: component dynamic-power breakdown and energy impact.
- `<kernel>_comparison`: temperature, total power and leakage versus time.
- `<kernel>_leakage_temperature_time`: exactly two quantities versus time,
  leakage (left axis) and temperature (right axis), for selected component instances.
- `<kernel>_thermal_maps`: matched end-of-run maps with shared absolute scale
  and a separate difference map. Maps are component-average approximations.
- `<kernel>_<profile>.gif`: optional animations using the existing renderer,
  with a shared color scale within each pair.
- `<kernel>_<profile>_cells.gif`: optional full finite-volume temperature maps;
  these resolve within-component gradients, unlike the floorplan averages.
- `cluster_floorplan`: annotated representative cluster layout.
- `model_assumptions`: temperature and technology scaling used by the models.

All static figures are supplied as high-resolution PNG and vector PDF.
`metrics.csv`, `component_energy.csv` and `timeseries.csv` provide slide-ready
numbers. `study.json` identifies raw run directories and artifact hashes.
`metrics.csv` distinguishes maximum component-average and finite-volume cell
temperatures. Tflp output has approximately 0.001 K precision, so differences
at that scale should not be overinterpreted.

![Component power and energy impact](workload_summary.png)

![Representative cluster floorplan](cluster_floorplan.png)

## Limitations

The SDK preload scripts use placeholder timing data, not independently checked
numerical golden tensors. Successful completion is therefore an execution
smoke test, not proof of kernel numerical accuracy. Approximate area, residual
clock budgets, lack of explicit gating, and coarse technology/leakage scaling
limit absolute accuracy. Collective reduction power is uncalibrated. The
floorplan is not derived from placement and routing. These results establish
an activity-aware thermal-feedback workflow; calibrated silicon prediction,
thermal steady state and accuracy across workloads require further evidence.
"""
    (args.output / "report.md").write_text(report)
    settings = {"repetitions_per_run": manifest.get("repeats", 1),
                "floorplan_rule": manifest["floorplan_rule"],
                "target_top_die_cells": manifest["target_cells"],
                "new_logic_estimate_multiplier": manifest["estimate_scale"],
                "coupling_interval_us_by_kernel": {
                    k: (pair[0].power_records[0]["window"]["end_ps"] - pair[0].power_records[0]["window"]["start_ps"]) * 1e-6
                    for k, pair in pairs.items()}}
    with (args.output / "report.md").open("a") as stream:
        stream.write("\n## Experiment settings\n\n```json\n" + json.dumps(settings, indent=2) + "\n```\n")
    print(f"Wrote analysis and validation results: {args.output}")


if __name__ == "__main__":
    main()
