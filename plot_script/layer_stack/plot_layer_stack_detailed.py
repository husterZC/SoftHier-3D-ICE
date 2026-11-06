#!/usr/bin/env python3
"""Draw a detailed face-to-face logic/DRAM package schematic.

This single file is portable: it needs only Python and Matplotlib, with no
repository, image, configuration, result, or separate font files. Geometry,
colors, thicknesses, and labels are embedded below. Heights and interconnect
counts are illustrative, not to scale. Defaults use
logic-on-DRAM order, 300 K coolant, and 25,000 W/(m²·K) cooling coefficient.

Examples:
    python plot_layer_stack_detailed.py
    python plot_layer_stack_detailed.py --output-dir ./figures
    python plot_layer_stack_detailed.py --stack-order dram_on_logic
    python plot_layer_stack_detailed.py --heat-transfer-coefficient 15000

Outputs: layer_stack_detailed_<order>.pdf, .svg, .png, and .json.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyBboxPatch, Rectangle

HERE = Path(__file__).resolve().parent

# Physical thicknesses in µm. These are a fixed copy of the reference values,
# not inputs loaded from the repository's current thermal configuration.
COPPER_UM = 50.0
TIM_LAYERS = 4
TIM_LAYER_UM = 7.5
DIES = {
    "logic": {"label": "Logic", "bulk_um": 49.0, "active_um": 1.0, "beol_um": 5.0,
              "bulk_color": "#D8E9F5", "active_color": "#287DB6", "accent": "#2688CD"},
    "dram": {"label": "DRAM", "bulk_um": 49.0, "active_um": 1.0, "beol_um": 5.0,
             "bulk_color": "#D5ECE7", "active_color": "#0A8072", "accent": "#008E80"},
}
HYBRID_BOND_UM = 2.0
PACKAGE_BUMPS_UM = 50.0
SUBSTRATE_LAYERS = 4
SUBSTRATE_LAYER_UM = 250.0

# Drawing coordinates are independent of physical thickness. Each source and
# BEOL layer is enlarged so it remains legible in a paper figure.
CANVAS_WIDTH, CANVAS_HEIGHT = 760.0, 665.0
STACK_X, STACK_WIDTH, STACK_TOP = 42.0, 490.0, 157.0
DISPLAY_HEIGHT = {
    "copper": 38.0, "tim": 38.0, "bulk": 70.0, "active": 25.0,
    "beol": 29.0, "hybrid": 19.0, "bumps": 38.0, "substrate": 104.0,
}
PAD_COUNT, BUMP_COUNT = 12, 11
PAD_AREA_FRACTION = .20
FONT = "DejaVu Sans"  # Bundled with Matplotlib.
INK, MUTED, EDGE = "#19374B", "#567491", "#8CA5B9"
COLORS = {
    "card": "#F6F8FC", "card_edge": "#D6E3F2", "copper": "#E7B77A",
    "tim": "#FAEDBE", "tim_line": "#C7BD94", "beol": "#ECD8B4",
    "hybrid": "#E9DFED", "pad": "#C7945B", "pad_edge": "#AD7D46",
    "bump": "#CCD9E3", "substrate": "#E6EEF4", "cooling": "#2583C3",
}


def number(value):
    return f"{value:,.6g}"


def text(ax, x, y, value, *, size=10, color=INK, align="center", **kwargs):
    return ax.text(x, y, value, ha=align, va="center", fontsize=size,
                   color=color, **kwargs)


def layer(ax, y, height, color):
    ax.add_patch(Rectangle((STACK_X, y), STACK_WIDTH, height,
                           facecolor=color, edgecolor=EDGE, linewidth=.65))


def bracket(ax, top, bottom, label, color):
    x = STACK_X + STACK_WIDTH + 12
    ax.plot([x, x + 10, x + 10, x], [top, top, bottom, bottom],
            color=color, linewidth=.8)
    text(ax, x + 24, (top + bottom) / 2, label, size=8.5, color=color,
         align="left", linespacing=1.35)


def leader(ax, y, label):
    x = STACK_X + STACK_WIDTH
    ax.plot([x, x + 25], [y, y], color=EDGE, linewidth=.7)
    text(ax, x + 36, y, label, size=8.5, color=MUTED, align="left", linespacing=1.35)


def arrow(ax, x, start_y, end_y, color):
    ax.annotate("", xy=(x, end_y), xytext=(x, start_y),
                arrowprops={"arrowstyle": "-|>", "color": color,
                            "linewidth": .8, "mutation_scale": 8, "shrinkA": 0, "shrinkB": 0})


def draw_die(ax, y, name, top_die):
    die = DIES[name]
    start = y
    parts = ("bulk", "active", "beol") if top_die else ("beol", "active", "bulk")
    for part in parts:
        height = DISPLAY_HEIGHT[part]
        if part == "bulk":
            label = f"{die['label']} bulk silicon · {number(die['bulk_um'])} µm"
            color, ink = die["bulk_color"], INK
        elif part == "active":
            label = f"{die['label']} active source · {number(die['active_um'])} µm"
            color, ink = die["active_color"], "white"
        else:
            label = f"{die['label']} BEOL · {number(die['beol_um'])} µm"
            color, ink = COLORS["beol"], INK
        layer(ax, y, height, color)
        text(ax, STACK_X + STACK_WIDTH / 2, y + height / 2, label,
             color=ink, size=9.3 if part == "active" else 10)
        y += height
    total = die["bulk_um"] + die["active_um"] + die["beol_um"]
    bracket(ax, start, y, f"{die['label']} die\n{number(total)} µm total", die["accent"])
    return y


def draw_stack(ax, order):
    top_name, bottom_name = (("logic", "dram") if order == "logic_on_dram" else ("dram", "logic"))
    y = STACK_TOP
    center = STACK_X + STACK_WIDTH / 2

    layer(ax, y, DISPLAY_HEIGHT["copper"], COLORS["copper"])
    text(ax, center, y + DISPLAY_HEIGHT["copper"] / 2, f"Copper · {number(COPPER_UM)} µm")
    for fraction in (.185, .5, .815):
        arrow(ax, STACK_X + fraction * STACK_WIDTH, y - 6, y - 24, COLORS["cooling"])
    text(ax, STACK_X + STACK_WIDTH + 26, y - 16, "Top cooling",
         size=8.5, color=MUTED, align="left")
    y += DISPLAY_HEIGHT["copper"]

    layer(ax, y, DISPLAY_HEIGHT["tim"], COLORS["tim"])
    for i in range(1, TIM_LAYERS):
        boundary = y + DISPLAY_HEIGHT["tim"] * i / TIM_LAYERS
        ax.plot([STACK_X, STACK_X + STACK_WIDTH], [boundary, boundary],
                color=COLORS["tim_line"], linewidth=.5)
    text(ax, center, y + DISPLAY_HEIGHT["tim"] / 2,
         f"TIM · {number(TIM_LAYERS * TIM_LAYER_UM)} µm",
         bbox={"facecolor": COLORS["tim"], "edgecolor": "none", "pad": 1.5})
    y += DISPLAY_HEIGHT["tim"]

    y = draw_die(ax, y, top_name, top_die=True)
    layer(ax, y, DISPLAY_HEIGHT["hybrid"], COLORS["hybrid"])
    pad_pitch = STACK_WIDTH / PAD_COUNT
    pad_width = pad_pitch * PAD_AREA_FRACTION
    for i in range(PAD_COUNT):
        x = STACK_X + (i + .5) * pad_pitch - pad_width / 2
        ax.add_patch(Rectangle((x, y + .7), pad_width, DISPLAY_HEIGHT["hybrid"] - 1.4,
                               facecolor=COLORS["pad"], edgecolor=COLORS["pad_edge"], linewidth=.4))
    bond_center = y + DISPLAY_HEIGHT["hybrid"] / 2
    leader(ax, bond_center, f"Hybrid bond\n{number(HYBRID_BOND_UM)} µm")
    f2f_x = CANVAS_WIDTH - 38
    arrow(ax, f2f_x, bond_center - 41, bond_center - 15, DIES[top_name]["accent"])
    arrow(ax, f2f_x, bond_center + 41, bond_center + 15, DIES[bottom_name]["accent"])
    text(ax, f2f_x, bond_center, "F2F", size=8, weight="bold")
    y += DISPLAY_HEIGHT["hybrid"]

    y = draw_die(ax, y, bottom_name, top_die=False)
    bump_center = y + DISPLAY_HEIGHT["bumps"] / 2
    radius = 15.5
    for i in range(BUMP_COUNT):
        x = STACK_X + (i + .5) * STACK_WIDTH / BUMP_COUNT
        ax.plot([x, x], [y, y + DISPLAY_HEIGHT["bumps"]], color=EDGE, linewidth=.5, zorder=1)
        ax.add_patch(Circle((x, bump_center), radius, facecolor=COLORS["bump"],
                            edgecolor=EDGE, linewidth=.8, zorder=2))
    leader(ax, bump_center, f"Package bumps\n{number(PACKAGE_BUMPS_UM)} µm")
    y += DISPLAY_HEIGHT["bumps"]

    layer(ax, y, DISPLAY_HEIGHT["substrate"], COLORS["substrate"])
    for i in range(1, SUBSTRATE_LAYERS):
        boundary = y + DISPLAY_HEIGHT["substrate"] * i / SUBSTRATE_LAYERS
        ax.plot([STACK_X, STACK_X + STACK_WIDTH], [boundary, boundary], color=EDGE, linewidth=.4)
    text(ax, center, y + DISPLAY_HEIGHT["substrate"] / 2,
         f"Substrate · {number(SUBSTRATE_LAYERS * SUBSTRATE_LAYER_UM)} µm\nSilicon in the thermal model",
         linespacing=1.55, bbox={"facecolor": COLORS["substrate"], "edgecolor": "none", "pad": 1.5})


def render(order, coolant_k, coefficient, output, dpi):
    top, bottom = (("Logic", "DRAM") if order == "logic_on_dram" else ("DRAM", "Logic"))
    style = {
        "font.family": FONT, "font.size": 10, "text.usetex": False,
        "pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "path",
        "svg.hashsalt": "detailed-layer-stack-v1", "savefig.facecolor": "white",
    }
    with plt.rc_context(style):
        figure = plt.figure(figsize=(CANVAS_WIDTH / 100, CANVAS_HEIGHT / 100), facecolor="white")
        ax = figure.add_axes([0, 0, 1, 1])
        ax.set(xlim=(0, CANVAS_WIDTH), ylim=(CANVAS_HEIGHT, 0), aspect="equal")
        ax.set_axis_off()
        ax.add_patch(FancyBboxPatch((6, 55), CANVAS_WIDTH - 12, CANVAS_HEIGHT - 63,
                                    boxstyle="round,pad=0,rounding_size=10", linewidth=.7,
                                    edgecolor=COLORS["card_edge"], facecolor=COLORS["card"]))
        text(ax, 14, 14, f"LLM stack · {top} on {bottom} · Schematic, not to scale",
             size=8.5, color=MUTED, align="left")
        text(ax, 14, 35, f"Cooling: {number(coolant_k)} K   |   h = {number(coefficient)} W/(m²·K)",
             size=8.5, color=MUTED, align="left")
        text(ax, CANVAS_WIDTH / 2, 85, "DRAM3D SoftHier", size=16, weight="bold")
        top_label = "logic" if top == "Logic" else top
        bottom_label = "logic" if bottom == "Logic" else bottom
        text(ax, CANVAS_WIDTH / 2, 110, f"One {top_label} die stacked on one {bottom_label} die",
             size=9.5, color=MUTED)
        draw_stack(ax, order)

        figure.canvas.draw()
        renderer = figure.canvas.get_renderer()
        for annotation in ax.texts:
            bounds = annotation.get_window_extent(renderer)
            if (bounds.x0 < -.5 or bounds.y0 < -.5 or bounds.x1 > figure.bbox.x1 + .5
                    or bounds.y1 > figure.bbox.y1 + .5):
                raise ValueError(f"Label extends outside the figure: {annotation.get_text()}")

        output = Path(output).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        stem = output / f"layer_stack_detailed_{order}"
        try:
            for extension in ("pdf", "svg", "png"):
                path = stem.with_suffix("." + extension)
                figure.savefig(path, dpi=dpi)
                print(path)
        finally:
            plt.close(figure)

    metadata = {
        "source": "embedded constants in plot_layer_stack_detailed.py; no input files",
        "stack_order": order, "coolant_temperature_k": coolant_k,
        "heat_transfer_coefficient_w_m2_k": coefficient,
        "physical_thickness_um": {
            "copper": COPPER_UM, "tim": TIM_LAYERS * TIM_LAYER_UM,
            "logic_bulk": DIES["logic"]["bulk_um"], "logic_active": DIES["logic"]["active_um"],
            "logic_beol": DIES["logic"]["beol_um"], "hybrid_bond": HYBRID_BOND_UM,
            "dram_bulk": DIES["dram"]["bulk_um"], "dram_active": DIES["dram"]["active_um"],
            "dram_beol": DIES["dram"]["beol_um"], "package_bumps": PACKAGE_BUMPS_UM,
            "substrate": SUBSTRATE_LAYERS * SUBSTRATE_LAYER_UM,
        },
        "tim_sublayers": TIM_LAYERS, "substrate_sublayers": SUBSTRATE_LAYERS,
        "to_scale": False, "pad_and_bump_counts": "illustrative",
        "font": FONT, "font_source": "bundled with Matplotlib", "png_dpi": dpi,
    }
    stem.with_suffix(".json").write_text(json.dumps(metadata, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(stem.with_suffix(".json"))


def positive_float(value):
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return parsed


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", "--output", type=Path, default=HERE,
                        help="Output directory; defaults to beside this script")
    parser.add_argument("--stack-order", choices=("logic_on_dram", "dram_on_logic"), default="logic_on_dram")
    parser.add_argument("--coolant-temperature", type=positive_float, default=300., metavar="K")
    parser.add_argument("--heat-transfer-coefficient", type=positive_float, default=25000., metavar="W_PER_M2_K")
    parser.add_argument("--dpi", type=positive_int, default=600, help="PNG resolution (default: 600)")
    args = parser.parse_args(argv)
    render(args.stack_order, args.coolant_temperature, args.heat_transfer_coefficient, args.output_dir, args.dpi)


if __name__ == "__main__":
    main()
