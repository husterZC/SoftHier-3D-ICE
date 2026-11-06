#!/usr/bin/env python3
"""Self-contained logic-die and expanded-cluster floorplan figures.

Copy this single file anywhere and run it with Python and Matplotlib. All
geometry, colors, and labels are embedded below; no repository, architecture,
data, or separate font files are needed. Outputs default to beside this script.
The embedded geometry is the DRAM3D square_bands layout at 5 nm, captured on
2026-11-06. Edit the constants below to change this fixed layout.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import PatchCollection
from matplotlib.patches import Rectangle
from matplotlib.ticker import MaxNLocator

# Fixed geometry of the current DRAM3D experiment. All lengths are in µm.
# These values reproduce the previous source-derived figures without loading
# the SDK, area models, a geometry JSON, or any other project file.
CLUSTER_GRID = (4, 4)  # columns, rows
CLUSTER_SHAPE_UM = (753.5607492673458, 753.5607492673458)

# name, kind, x, y, width, height
COMPONENTS = (
    ("redmule", "light_redmule", 0.0, 0.0, 569.5421121829553, 358.98694309807814),
    ("tcdm", "memory", 569.5421121829553, 0.0, 184.01863708439055, 358.98694309807814),
    ("spatz0", "spatz", 0.0, 358.98694309807814, 181.58188580592721, 236.9329246790041),
    ("pe0", "core", 0.0, 595.9198677770822, 181.58188580592721, 41.80433008071176),
    ("spatz1", "spatz", 181.58188580592721, 358.98694309807814, 181.58188580592721, 236.9329246790041),
    ("pe1", "core", 181.58188580592721, 595.9198677770822, 181.58188580592721, 41.80433008071176),
    ("spatz2", "spatz", 363.16377161185443, 358.98694309807814, 181.58188580592721, 236.9329246790041),
    ("pe2", "core", 363.16377161185443, 595.9198677770822, 181.58188580592721, 41.80433008071176),
    ("spatz3", "spatz", 544.7456574177817, 358.98694309807814, 181.58188580592721, 236.9329246790041),
    ("pe3", "core", 544.7456574177817, 595.9198677770822, 181.58188580592721, 41.80433008071176),
    ("pe4", "core", 726.3275432237089, 358.98694309807814, 27.233206043637043, 278.7372547597158),
    ("instr_mem", "memory", 0.0, 637.724197857794, 95.04813347794573, 115.83655140955179),
    ("stack_mem", "memory", 95.04813347794573, 637.724197857794, 190.09626695589145, 115.83655140955179),
    ("idma", "idma", 285.1444004338372, 637.724197857794, 182.31017378544325, 115.83655140955179),
    ("data_noc_router", "floonoc", 467.4545742192804, 637.724197857794, 125.56878091119637, 115.83655140955179),
    ("data_noc_ni", "floonoc", 593.0233551304768, 637.724197857794, 17.2657073752895, 115.83655140955179),
    ("sync_bus_router", "floonoc", 610.2890625057663, 637.724197857794, 10.006262228860962, 115.83655140955179),
    ("sync_bus_ni", "floonoc", 620.2953247346272, 637.724197857794, 5.101231724517353, 115.83655140955179),
    ("transpose_engine", "transpose", 625.3965564591446, 637.724197857794, 5.933124898054029, 115.83655140955179),
    ("others", "unmodeled", 631.3296813571986, 637.724197857794, 122.2310679101472, 115.83655140955179),
)

# Keep the colors used in the existing thermal-replay floorplan figures.
KINDS = {
    "core": ("Scalar cores", "#0072B2"),
    "spatz": ("Spatz", "#009E73"),
    "light_redmule": ("RedMulE", "#D55E00"),
    "memory": ("SRAM", "#56B4E9"),
    "idma": ("iDMA", "#CC79A7"),
    "floonoc": ("NoC + NI", "#E69F00"),
    "transpose": ("Transpose", "#777777"),
    "unmodeled": ("Others", "#EEEEEE"),
}
LABELS = {
    "redmule": "RedMulE", "tcdm": "TCDM", "instr_mem": "Instr.\nSRAM",
    "stack_mem": "Stack\nSRAM", "idma": "iDMA", "data_noc_router": "Data\nrouter",
    "data_noc_ni": "Data NoC NI", "sync_bus_router": "Sync router",
    "sync_bus_ni": "Sync NI", "transpose_engine": "Transpose engine",
    "others": "Others",
}


def build_geometry():
    """Build and check the embedded layout, without opening input files."""
    columns, rows = CLUSTER_GRID
    shape = list(CLUSTER_SHAPE_UM)
    if any(type(value) is not int or value <= 0 for value in CLUSTER_GRID):
        raise ValueError("CLUSTER_GRID must contain positive integer columns and rows")
    if any(not math.isfinite(value) or value <= 0 for value in shape):
        raise ValueError("CLUSTER_SHAPE_UM must contain positive finite dimensions")
    components, names, placed = [], set(), []
    tolerance = max(shape) * 1e-10
    for name, kind, x, y, width, height in COMPONENTS:
        if name in names or kind not in KINDS:
            raise ValueError(f"Duplicate component or unknown kind: {name}, {kind}")
        names.add(name)
        if (any(not math.isfinite(v) for v in (x, y, width, height))
                or min(width, height) <= 0 or min(x, y) < -tolerance
                or x + width > shape[0] + tolerance or y + height > shape[1] + tolerance):
            raise ValueError(f"Invalid component rectangle: {name}")
        for other_name, ox, oy, ow, oh in placed:
            overlap = (max(0., min(x + width, ox + ow) - max(x, ox))
                       * max(0., min(y + height, oy + oh) - max(y, oy)))
            if overlap > 1e-7:
                raise ValueError(f"Overlapping components: {name}, {other_name}")
        placed.append((name, x, y, width, height))
        components.append(dict(name=name, kind=kind, area_um2=width * height,
                               offset_um=[x, y], shape_um=[width, height]))
    if not math.isclose(sum(item["area_um2"] for item in components),
                        shape[0] * shape[1], rel_tol=1e-9):
        raise ValueError("Component rectangles must cover the cluster area")
    return {
        "source": "geometry embedded in plot_floorplans.py; no input files",
        "geometry_version": "dram3d-square-bands-2026-11-06",
        "floorplan_rule": "square_bands",
        "technology_node": "5nm",
        "layout_note": "Area-estimated floorplan; dimensions are in micrometers.",
        "cluster_grid": {"columns": columns, "rows": rows, "count": columns * rows},
        "cluster_shape_um": shape,
        "logic_die_shape_um": [columns * shape[0], rows * shape[1]],
        "cluster_components": components,
    }


def configure_style():
    # This font ships with Matplotlib; no separate installation or file needed.
    family = "DejaVu Serif"
    plt.rcParams.update({
        "font.family": family, "font.size": 10,
        "axes.linewidth": .7, "axes.grid": False,
        "axes.unicode_minus": False, "axes.titlesize": 12,
        "xtick.major.width": .7, "ytick.major.width": .7,
        "pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "path",
        "svg.hashsalt": "softhier-standalone-floorplan-v2",
        "savefig.facecolor": "white",
    })
    return {"family": family, "source": "bundled with Matplotlib"}


def handles(geometry):
    present = {item["kind"] for item in geometry["cluster_components"]}
    return [Rectangle((0, 0), 1, 1, facecolor=color, edgecolor="#BBBBBB",
                      linewidth=.3, label=label)
            for kind, (label, color) in KINDS.items() if kind in present]


def setup_axes(ax, shape_um):
    ax.set(xlim=(0, shape_um[0] / 1000), ylim=(0, shape_um[1] / 1000),
           aspect="equal", xlabel="x (mm)", ylabel="y (mm)")
    ax.xaxis.set_major_locator(MaxNLocator(nbins=6))
    ax.yaxis.set_major_locator(MaxNLocator(nbins=6))
    ax.spines[["top", "right"]].set_visible(False)


def plot_logic_die(geometry):
    fig, ax = plt.subplots(figsize=(5.2, 5.8))
    fig.subplots_adjust(left=.13, right=.98, bottom=.22, top=.93)
    patches, colors = [], []
    width, height = geometry["cluster_shape_um"]
    for row in range(geometry["cluster_grid"]["rows"]):
        for column in range(geometry["cluster_grid"]["columns"]):
            for item in geometry["cluster_components"]:
                x, y = item["offset_um"]
                w, h = item["shape_um"]
                patches.append(Rectangle(((x + column * width) / 1000,
                                          (y + row * height) / 1000), w / 1000, h / 1000))
                colors.append(KINDS[item["kind"]][1])
    ax.add_collection(PatchCollection(patches, facecolors=colors,
                                     edgecolors="white", linewidth=.3))
    setup_axes(ax, geometry["logic_die_shape_um"])
    ax.set_title("Logic die", pad=10)
    ax.legend(handles=handles(geometry), loc="upper center", bbox_to_anchor=(.5, -.15),
              ncol=3, frameon=False, fontsize=9, columnspacing=1.3, handlelength=1.2)
    return fig


def plot_cluster(geometry):
    fig = plt.figure(figsize=(7.6, 5.0))
    ax = fig.add_axes([.085, .13, .54, .77])
    key = fig.add_axes([.68, .13, .30, .77])
    key.set_axis_off()
    width, height = [value / 1000 for value in geometry["cluster_shape_um"]]
    callouts = []
    for item in geometry["cluster_components"]:
        x, y = [value / 1000 for value in item["offset_um"]]
        w, h = [value / 1000 for value in item["shape_um"]]
        ax.add_patch(Rectangle((x, y), w, h, facecolor=KINDS[item["kind"]][1],
                               edgecolor="white", linewidth=.6, alpha=.8))
        rotate = w < width * .086 and h > height * .20
        if min(w / width, h / height) < .046 and not rotate:
            callouts.append((x + w / 2, y + h, item["name"]))
        else:
            ax.text(x + w / 2, y + h / 2, LABELS.get(item["name"], item["name"]),
                    ha="center", va="center", fontsize=9, rotation=90 if rotate else 0)
    # Place the tiny-region numbers above the cluster with separated leaders,
    # instead of squeezing several overlapping labels into the top band.
    callouts.sort()
    if callouts:
        spacing = width * .045
        positions = []
        for x, _, _ in callouts:
            positions.append(max(x, positions[-1] + spacing) if positions else x)
        shift = max(0., positions[-1] - width * .98)
        for number, ((x, y, _), label_x) in enumerate(zip(callouts, positions), 1):
            ax.annotate(str(number), xy=(x, y), xytext=(label_x - shift, height * 1.035),
                        ha="center", va="bottom", fontsize=8, annotation_clip=False,
                        arrowprops={"arrowstyle": "-", "linewidth": .5, "color": "#555555",
                                    "shrinkA": 1, "shrinkB": 0})
        key.text(0, .98, "Small regions", weight="bold", va="top")
        for number, (_, _, name) in enumerate(callouts, 1):
            label = LABELS.get(name, name.replace("_", " ")).replace("\n", " ")
            key.text(0, .92 - (number - 1) * .055, f"{number}. {label}", va="top", fontsize=9)
    legend_top = .92 - len(callouts) * .055 - .06 if callouts else .98
    key.legend(handles=handles(geometry), loc="upper left", bbox_to_anchor=(0, legend_top),
               frameon=False, fontsize=9, borderaxespad=0, handlelength=1.1, labelspacing=.8)
    key.text(0, .015, f"Area-estimated layout\n{geometry['cluster_grid']['count']} identical clusters",
             va="bottom", fontsize=8, color="#555555")
    setup_axes(ax, geometry["cluster_shape_um"])
    fig.suptitle("Expanded cluster floorplan", fontsize=12, y=.98)
    return fig


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", "--output", type=Path, default=HERE,
                        help="Output directory (default: beside this script); matching exports are overwritten")
    parser.add_argument("--dpi", type=positive_int, default=600, help="PNG resolution (default: 600)")
    args = parser.parse_args(argv)
    geometry = build_geometry()
    geometry["font"] = configure_style()
    geometry["png_dpi"] = args.dpi
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name, plot in (("logic_die_floorplan", plot_logic_die), ("cluster_floorplan", plot_cluster)):
        figure = plot(geometry)
        try:
            for extension in ("pdf", "png", "svg"):
                path = output / f"{name}.{extension}"
                figure.savefig(path, dpi=args.dpi, bbox_inches="tight", pad_inches=.06)
                print(path)
        finally:
            plt.close(figure)
    metadata = output / "floorplan_geometry.json"
    metadata.write_text(json.dumps(geometry, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(metadata)
    print(f"{geometry['cluster_grid']['count']} clusters; "
          f"cluster {geometry['cluster_shape_um'][0]:.3f} × {geometry['cluster_shape_um'][1]:.3f} µm; "
          f"logic die {geometry['logic_die_shape_um'][0] / 1000:.4f} × "
          f"{geometry['logic_die_shape_um'][1] / 1000:.4f} mm")


if __name__ == "__main__":
    main()
