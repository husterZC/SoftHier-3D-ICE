#!/usr/bin/env python3
"""Publication figure from a completed optimized closed-loop overhead study.

Read-only with respect to the study. Exports PDF/SVG/PNG, plotted data,
provenance, and a caption beside this script unless --output-dir is supplied.
No simulator, build, or statistical samples from pilot runs are involved.
"""

import argparse
from collections import defaultdict
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DEFAULT_STUDY = ROOT / "experiments/closed_loop_overhead/results/b8_overhead_optimized"
FONT_FAMILY = "Times New Roman"
STEM = "closed_loop_overhead"
SCOPES = ("roi_seconds",)
FIGURE_SIZE = (3.6, 2.85)
STYLE_VERSION = "closed-loop-overhead-publication-v2"
# Flat, restrained fills: outlines/hatching can obscure sub-minute segments.
PHASE_STYLE = {
    "architecture": ("SoftHier execution", "#4C7594", ""),
    "power_estimation": ("Power estimation", "#E7BC55", ""),
    "thermal_advancement": ("3D-ICE thermal steps", "#C2764B", ""),
    "coupling_output": ("Coordination + output", "#315D54", ""),
}
PHASES = tuple(PHASE_STYLE)

os.environ.setdefault("MPLCONFIGDIR", "/tmp/closed-loop-overhead-mpl")
sys.path.insert(0, str(ROOT))
from experiments.closed_loop_overhead.analyze import (  # noqa: E402
    block_statistics, partition, power_accounting_counter, read_events,
)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def select_blocks(rows, names):
    """Keep complete paired blocks, never pilots or partial comparisons."""
    groups = defaultdict(dict)
    for row in rows:
        if row["pilot"] or row["status"] != "complete":
            continue
        name = row["configuration"]
        if name not in names:
            raise ValueError(f"unexpected configuration: {name}")
        if name in groups[row["block"]]:
            raise ValueError(f"duplicate {name} in measured block {row['block']}")
        groups[row["block"]][name] = row
    blocks = [b for _, b in sorted(groups.items()) if set(b) == set(names)]
    if not blocks:
        raise ValueError("no complete measured block; pilots and partial runs cannot be plotted")
    return blocks


def exclusive_phases(row, events):
    """Combine directly measured inline accounting with exclusive capture/mapping."""
    starts = [e for e in events if e["phase"] == "roi_start"]
    ends = [e for e in events if e["phase"] == "roi_end"]
    if len(starts) != 1 or len(ends) != 1:
        raise ValueError(f"{row['id']}: expected exactly one workload start/end pair")
    roi = starts[0]["start_ns"], ends[0]["end_ns"]
    if not row["start_ns"] <= roi[0] < roi[1] <= row["end_ns"]:
        raise ValueError(f"{row['id']}: workload markers outside the measured run")
    if ends[0]["sim_ps"] - starts[0]["sim_ps"] != row["timing"]["roi_simulated_ps"]:
        raise ValueError(f"{row['id']}: simulated workload duration changed")
    counter = power_accounting_counter(events, roi, required=True)
    if (counter != row["timing"].get("power_accounting") or
            counter["pid"] != starts[0]["pid"] or counter["pid"] != ends[0]["pid"] or
            counter["window_start_ps"] != starts[0]["sim_ps"] or
            counter["window_end_ps"] != ends[0]["sim_ps"]):
        raise ValueError(f"{row['id']}: direct inline timing disagrees with measurement or workload markers")
    # Existing precedence is retained: thermal steps/output can overlap mapping.
    refined = [dict(e, phase="power_capture") if e["phase"] == "power_mapping" else e
               for e in events]
    scope = SCOPES[0]
    partitioned = partition(refined, *roi, roi)
    phases = {phase: partitioned[phase] for phase in PHASES if phase != "power_estimation"}
    phases["power_estimation"] = partitioned["inline_power_accounting"] + partitioned["power_capture"]
    total = row["timing"][scope]
    if not math.isfinite(total) or total <= 0 or not math.isclose(
            sum(phases.values()), total, rel_tol=1e-10, abs_tol=1e-8):
        raise ValueError(f"{row['id']}: {scope} does not reconcile with raw timing")
    if any(not math.isfinite(value) or value < 0 for value in phases.values()):
        raise ValueError(f"{row['id']}: invalid phase duration")
    return {scope: phases, "power_components_seconds": {
        "inline_power_accounting": counter["seconds"], "power_capture_mapping": partitioned["power_capture"]}}


def summarize(blocks, names, pitches, phase_values, seed):
    rows = [b[name] for b in blocks for name in names]
    stats, _ = block_statistics(rows, names, samples=10000, seed=seed)
    lookup = {(s["configuration"], s["scope"]): s for s in stats}
    result = []
    for scope in SCOPES:
        for name in names:
            stat = lookup[name, scope]
            phases = {phase: statistics.mean(phase_values[b[name]["id"]][scope][phase]
                                            for b in blocks) for phase in PHASES}
            if not math.isclose(sum(phases.values()), stat["mean_seconds"], abs_tol=1e-8):
                raise ValueError("mean phase durations do not sum to mean runtime")
            components = {key: statistics.mean(phase_values[b[name]["id"]]["power_components_seconds"][key]
                                               for b in blocks)
                          for key in ("inline_power_accounting", "power_capture_mapping")}
            if not math.isclose(sum(components.values()), phases["power_estimation"], abs_tol=1e-8):
                raise ValueError("power components do not reconcile with the yellow block")
            ci = stat["slowdown_ci95"]
            result.append(dict(
                configuration=name, scope=scope, grid_pitch_um=pitches[name],
                native_cells=blocks[0][name]["timing"]["native_cells"],
                complete_blocks=len(blocks), runtime_seconds=stat["mean_seconds"],
                runtime_minutes=stat["mean_seconds"] / 60, slowdown=stat["slowdown"],
                overhead_percent=stat["overhead_percent"],
                runtime_ci95_seconds=stat["mean_ci95"],
                overhead_ci95_percent=[100 * (x - 1) for x in ci] if ci else None,
                phases_seconds=phases, power_components_seconds=components))
    return result


def load_study(study):
    study = Path(study).resolve()
    manifest = json.loads((study / "study.json").read_text())
    settings = manifest["settings"]
    if settings.get("power_accounting_timing") is not True:
        raise ValueError("this study has no direct inline-power measurements; run the existing experiment "
                         "with default power-accounting timing before generating the combined figure")
    if settings.get("coupling_mode") != "optimized":
        raise ValueError("this figure requires an optimized full study, not legacy or bounded diagnostics")
    pitches = manifest["configurations"]
    if len(pitches) != 4 or pitches.get("architecture_only", "missing") is not None:
        raise ValueError("expected architecture only and three thermal-grid configurations")
    for name, pitch in pitches.items():
        if name != "architecture_only" and (not isinstance(pitch, (int, float)) or
                                            not math.isfinite(pitch) or pitch <= 0):
            raise ValueError(f"invalid grid pitch for {name}")
    names = ["architecture_only", *sorted((n for n in pitches if n != "architecture_only"),
                                          key=lambda n: pitches[n], reverse=True)]
    if not math.isclose(settings["thermal_step_seconds"] * 1e12, settings["interval_ps"], abs_tol=.5):
        raise ValueError("expected equal thermal-step and power-update intervals")
    sources = {str(study / "study.json"): sha256(study / "study.json")}
    rows, excluded = [], []
    for item in manifest["schedule"]:
        if item["pilot"]:
            excluded.append(dict(id=item["id"], reason="pilot"))
            continue
        path = study / "runs" / item["id"] / "measurement.json"
        if not path.exists():
            excluded.append(dict(id=item["id"], reason="missing measurement"))
            continue
        row = json.loads(path.read_text())
        sources[str(path)] = sha256(path)
        if any(row[key] != item[key] for key in ("id", "configuration", "block", "pilot")):
            raise ValueError(f"{path}: measurement disagrees with the scheduled case")
        rows.append(row)
    blocks = select_blocks(rows, names)
    chosen = {row["id"] for b in blocks for row in b.values()}
    excluded.extend(dict(id=r["id"], reason="incomplete block" if r["status"] == "complete" else r["status"])
                    for r in rows if r["id"] not in chosen)
    signature = manifest["architectural_signature"]
    phase_values = {}
    for b in blocks:
        for name, row in b.items():
            if row.get("power_accounting_timing") is not True:
                raise ValueError(f"{row['id']}: power-accounting timing was not enabled")
            v, t = row["validation"], row["timing"]
            if (v["status"] != "PASS" or v["cycles"] != signature["cycles"] or
                    v["stage_cycles"] != signature["stage_cycles"] or
                    t["roi_simulated_ps"] != signature["roi_simulated_ps"]):
                raise ValueError(f"{row['id']}: failed validation or architectural signature mismatch")
            if t["native_cells"] != blocks[0][name]["timing"]["native_cells"]:
                raise ValueError(f"{name}: thermal mesh changed between repetitions")
            directory = study / "runs" / row["id"] / "timing"
            events = read_events(directory)
            phase_values[row["id"]] = exclusive_phases(row, events)
            sources.update({str(p): sha256(p) for p in sorted(directory.glob("*.jsonl"))})
    data = summarize(blocks, names, pitches, phase_values, settings["seed"])
    baseline = blocks[0]["architecture_only"]["validation"]
    return dict(study=str(study), settings=settings, configurations=names, data=data,
                complete_blocks=len(blocks), included_runs=sorted(chosen), excluded_runs=excluded,
                architectural_signature=signature, input_sha256=sources,
                workload=dict(batch=baseline["batch"], past_kv=baseline["past_kv"], layer=baseline["layer"]),
                phase_definitions={
                    "architecture": "SoftHier execution excluding directly timed inline power routines",
                    "power_estimation": "Direct inline power accounting plus exclusive native capture and adapter mapping",
                    "thermal_advancement": "Exclusive 3D-ICE thermal steps during the workload",
                    "coupling_output": "Remaining coordination and output inside the workload"},
                measurement_scope="roi_seconds", layout="single_panel",
                attribution="Raw interval partition plus direct ROI counter; nested inline calls counted once, "
                            "hook work excluded; power mapping retains existing overlap priorities",
                timing_method_version=1,
                instrumentation="Wall times include timer overhead; no estimated correction is subtracted",
                statistics="Means and ratio of means; 10,000 paired-block bootstrap resamples when n >= 2; pilots excluded",
                bootstrap_seed=settings["seed"])


def register_fonts(font_directory):
    from matplotlib import font_manager
    directories = [Path(font_directory)] if font_directory else [HERE / ".font-cache", HERE.parent / "layer_stack/.font-cache"]
    if font_directory and not Path(font_directory).is_dir():
        raise ValueError(f"font directory does not exist: {font_directory}")
    for directory in directories:
        if directory.is_dir():
            for path in sorted(directory.rglob("*")):
                if path.suffix.lower() in (".ttf", ".otf", ".ttc"):
                    font_manager.fontManager.addfont(str(path))
    fonts = {}
    for weight in ("normal", "bold"):
        try:
            path = Path(font_manager.findfont(font_manager.FontProperties(
                family=FONT_FAMILY, weight=weight), fallback_to_default=False)).resolve()
        except ValueError as error:
            raise ValueError("Times New Roman regular/bold fonts are required; use --font-dir") from error
        fonts[weight] = dict(path=str(path), sha256=sha256(path))
    return fonts


def caption(report):
    w, settings, n = report["workload"], report["settings"], report["complete_blocks"]
    grids = [r for r in report["data"] if r["scope"] == SCOPES[0] and r["grid_pitch_um"] is not None]
    meshes = ", ".join(f"{r['grid_pitch_um']:g} µm ({r['native_cells']:,} cells)" for r in grids)
    repetitions = ("One measured repetition per configuration; no confidence intervals are shown."
                   if n == 1 else f"Means of {n} complete measured blocks; whiskers show 95% paired-block bootstrap confidence intervals (10,000 resamples).")
    return (f"Closed-loop simulation overhead for B{w['batch']} LLM decode with KV-cache length {w['past_kv']} "
            f"and layer {w['layer']}, using optimized coupling. Architecture-only execution is compared with "
            f"thermal grids of {meshes}. Thermal steps and power updates both use {settings['interval_ps'] / 1e6:g} µs. "
            "Bars show exclusive host wall-clock durations during workload execution, excluding "
            "initialization and finalization. Labels give runtime in minutes "
            "and percentage overhead relative to architecture-only workload execution, calculated as "
            "100 × (mean case runtime / mean baseline runtime − 1). Yellow combines directly timed inline "
            "logic/DRAM power accounting with exclusive power capture and mapping. Inline timing includes "
            "power-maintenance callbacks and scheduler calls within power routines, counts nested calls once, "
            "and excludes hook exchanges. Blue is the remaining SoftHier execution, including scheduler "
            "work outside these routines. Timings include measurement overhead without an estimated correction. "
            "Mapping overlaps are resolved using the original timing "
            f"partition priorities. {repetitions} Pilots are excluded. All included runs passed numerical "
            "validation and share the same architectural cycle signature.\n")


def write_table(path, data):
    rows = []
    for record in data:
        row = {k: v for k, v in record.items() if k not in
               ("phases_seconds", "power_components_seconds", "runtime_ci95_seconds", "overhead_ci95_percent")}
        for key in ("runtime_ci95_seconds", "overhead_ci95_percent"):
            ci = record[key]
            row[key + "_low"], row[key + "_high"] = ci if ci else (None, None)
        row.update({phase + "_seconds": value for phase, value in record["phases_seconds"].items()})
        row.update({phase + "_seconds": value for phase, value in record["power_components_seconds"].items()})
        rows.append(row)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def draw(report, font_directory=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch
    from matplotlib.text import Text
    from matplotlib.ticker import MaxNLocator

    fonts = register_fonts(font_directory)
    style = {
        "font.family": FONT_FAMILY, "font.size": 8, "text.usetex": False,
        "pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "path",
        "svg.hashsalt": STYLE_VERSION, "axes.unicode_minus": False,
        "figure.facecolor": "white", "savefig.facecolor": "white",
        "text.color": "#252A2E", "axes.labelcolor": "#252A2E",
        "xtick.color": "#343B40", "ytick.color": "#525B62",
    }
    with plt.rc_context(style):
        fig, ax = plt.subplots(figsize=FIGURE_SIZE)
        fig.subplots_adjust(left=.16, right=.985, bottom=.24, top=.81)
        largest = max(max(r["runtime_ci95_seconds"] or [r["runtime_seconds"]]) / 60
                      for r in report["data"])
        ymax = math.ceil(largest * 1.16 / 10) * 10
        annotations = []
        records = report["data"]
        # A small gap separates the architecture baseline from the three grids.
        positions = [0., *[i + .15 for i in range(1, len(records))]]
        bottom = [0.] * len(records)
        for phase in PHASES:
            _, color, hatch = PHASE_STYLE[phase]
            for i, record in enumerate(records):
                height = record["phases_seconds"][phase] / 60
                if height > 0:
                    ax.bar(positions[i], height, bottom=bottom[i], width=.62,
                           color=color, edgecolor="none", linewidth=0, hatch=hatch, zorder=3)
                bottom[i] += height
        for i, record in enumerate(records):
            x = positions[i]
            top = record["runtime_minutes"]
            ci = record["runtime_ci95_seconds"]
            if ci is not None:
                low, high = (v / 60 for v in ci)
                ax.vlines(x, low, high, color="#252A2E", linewidth=.65, zorder=4)
                ax.hlines([low, high], x - .06, x + .06, color="#252A2E", linewidth=.65, zorder=4)
                top = max(top, high)
            overhead = "baseline" if record["configuration"] == "architecture_only" else f"{record['overhead_percent']:+.1f}%"
            annotations.append(ax.annotate(f"{record['runtime_minutes']:.1f}", (x, top),
                xytext=(0, 12), textcoords="offset points", ha="center", va="bottom",
                fontsize=8.6, fontweight="bold", color="#252A2E", annotation_clip=False))
            annotations.append(ax.annotate(overhead, (x, top),
                xytext=(0, 2.5), textcoords="offset points", ha="center", va="bottom",
                fontsize=7.2, color="#626A70", annotation_clip=False))
        labels = ["Architecture\nonly" if r["grid_pitch_um"] is None else
                  f"{r['grid_pitch_um']:g} µm" for r in records]
        ax.set_xticks(positions, labels)
        ax.set(xlim=(positions[0] - .58, positions[-1] + .58), ylim=(0, ymax))
        ticks = MaxNLocator(nbins=5, steps=[1, 2, 2.5, 5, 10]).tick_values(0, ymax)
        ax.set_yticks([tick for tick in ticks if 0 <= tick <= ymax])
        ax.set_axisbelow(True)
        ax.yaxis.grid(True, color="#E0E4E7", linewidth=.4)
        ax.tick_params(axis="x", length=0, pad=5, labelsize=7.8)
        ax.tick_params(axis="y", width=.45, length=2.5, pad=3, labelsize=7.5)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.spines["left"].set_linewidth(.45)
        ax.spines["left"].set_color("#A3ABB1")
        ax.spines["bottom"].set_linewidth(.6)
        ax.spines["bottom"].set_color("#59636B")
        ax.set_ylabel("Host wall time (min)", fontsize=8.2, labelpad=5)
        # Matplotlib fills legend columns first; reorder for stack order by row.
        order = [PHASES[i] for i in (0, 2, 1, 3)]
        handles = [Patch(facecolor=PHASE_STYLE[p][1], edgecolor="none",
                         label=PHASE_STYLE[p][0]) for p in order]
        legend = fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(.57, .99), ncol=2,
                   frameon=False, fontsize=7.4, handlelength=1.1, handleheight=.8,
                   handletextpad=.55, columnspacing=1.6, labelspacing=.6, borderpad=0)
        transform = ax.get_xaxis_transform()
        left, right = positions[1] - .31, positions[-1] + .31
        ax.plot([left, left, right, right], [-.105, -.135, -.135, -.105],
                transform=transform, color="#969FA6", linewidth=.45, clip_on=False)
        group_label = ax.text((left + right) / 2, -.17, "Closed loop", transform=transform,
                             ha="center", va="top", fontsize=7.5, color="#525B62")
        w, settings, n = report["workload"], report["settings"], report["complete_blocks"]
        note = "n = 1 per case" if n == 1 else f"n = {n}; 95% CIs"
        fig.text(.16, .025, f"B{w['batch']} · KV {w['past_kv']} · Δt = {settings['interval_ps'] / 1e6:g} µs",
                 fontsize=6.8, color="#626A70", va="bottom")
        fig.text(.985, .025, note, ha="right", va="bottom", fontsize=6.8, color="#626A70")
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        for text in fig.findobj(Text):
            if text.get_visible() and text.get_text():
                box = text.get_window_extent(renderer)
                if not fig.bbox.padded(1).contains(box.x0, box.y0) or not fig.bbox.padded(1).contains(box.x1, box.y1):
                    plt.close(fig)
                    raise ValueError(f"text is clipped by the figure canvas: {text.get_text()}")
        boxes = [text.get_window_extent(renderer) for text in annotations]
        if any(a.overlaps(b) for i, a in enumerate(boxes) for b in boxes[i + 1:]):
            plt.close(fig)
            raise ValueError("runtime annotations overlap")
        if any(box.overlaps(legend.get_window_extent(renderer)) for box in boxes):
            plt.close(fig)
            raise ValueError("runtime annotations overlap the legend")
        if any(group_label.get_window_extent(renderer).overlaps(t.get_window_extent(renderer))
               for t in ax.get_xticklabels()):
            plt.close(fig)
            raise ValueError("closed-loop group label overlaps case labels")
    return fig, fonts


def render(study, output, dpi=600, font_directory=None):
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    report = load_study(study)
    fig, fonts = draw(report, font_directory)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    stem = output / STEM
    import matplotlib
    import matplotlib.pyplot as plt
    try:
        # Saving also needs the export/font settings: draw() uses a scoped style.
        with matplotlib.rc_context({"pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "path",
                                    "svg.hashsalt": STYLE_VERSION}):
            for extension in ("pdf", "svg", "png"):
                metadata = dict(Title="Closed-loop simulation overhead", Creator=Path(__file__).name)
                if extension == "pdf":
                    metadata.update(CreationDate=None, ModDate=None)
                elif extension == "svg":
                    metadata["Date"] = None
                fig.savefig(stem.with_suffix("." + extension), dpi=dpi, facecolor="white", metadata=metadata)
    finally:
        plt.close(fig)
    report.update(figure_size_inches=list(FIGURE_SIZE), png_dpi=dpi, font_family=FONT_FAMILY,
                  style_version=STYLE_VERSION,
                  phase_style={key: dict(label=value[0], color=value[1]) for key, value in PHASE_STYLE.items()},
                  font_files=fonts, svg_text_as_paths=True, matplotlib_version=matplotlib.__version__,
                  script_sha256=sha256(__file__),
                  timing_analysis_sha256=sha256(ROOT / "experiments/closed_loop_overhead/analyze.py"))
    write_table(stem.with_suffix(".csv"), report["data"])
    write_json(stem.with_suffix(".json"), report)
    (output / (STEM + "_caption.txt")).write_text(caption(report))
    for extension in ("pdf", "svg", "png", "csv", "json"):
        print(stem.with_suffix("." + extension))
    print(output / (STEM + "_caption.txt"))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, default=DEFAULT_STUDY, help="completed optimized study directory")
    parser.add_argument("--output-dir", type=Path, default=HERE)
    parser.add_argument("--dpi", type=int, default=600)
    parser.add_argument("--font-dir", type=Path, help="directory containing Times New Roman font files")
    args = parser.parse_args()
    try:
        render(args.study, args.output_dir, args.dpi, args.font_dir)
    except (ValueError, FileNotFoundError, KeyError) as error:
        parser.exit(2, f"error: {error}\n")


if __name__ == "__main__":
    main()
