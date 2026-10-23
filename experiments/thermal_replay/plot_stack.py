"""Draw an equal-height schematic from a saved replay's actual thermal stack."""

import argparse
import math
import os
from pathlib import Path
import re

from .activity import digest, write_json


NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
NAME = r"[A-Za-z_][A-Za-z_0-9]*"


def uncomment(text):
    return re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)


def positive(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("stack dimensions and material properties must be finite and positive")
    return value


def read_stack(path):
    """Read passive layers and dies with one source plus bulk/BEOL sublayers.

    Reject unsupported stack elements/die layouts instead of omitting physical
    layers. The returned data contains no filenames, so pairs can be compared.
    """
    text = uncomment(Path(path).read_text())
    headers = list(re.finditer(r"(?m)^[ \t]*([A-Za-z_][\w \t]*?)\s*:", text))
    sections = {}
    for i, match in enumerate(headers):
        key = " ".join(match[1].split())
        if key in sections:
            raise ValueError(f"duplicate stack section: {key}")
        sections[key] = text[match.end():headers[i + 1].start() if i + 1 < len(headers) else len(text)].strip()

    def match_section(key, pattern, full=False):
        match = (re.fullmatch if full else re.search)(pattern, sections.get(key, ""))
        if match is None:
            raise ValueError(f"missing or unsupported stack section: {key}")
        return match

    materials, definitions = {}, {}
    for key, body in sections.items():
        kind, _, name = key.partition(" ")
        if kind == "material":
            match = match_section(key, rf"thermal conductivity\s+({NUMBER}(?:\s*,\s*{NUMBER})*)\s*;\s*"
                                  rf"volumetric heat capacity\s+({NUMBER})\s*;", full=True)
            conductivity = [positive(v) * 1e6 for v in match[1].split(",")]
            if len(conductivity) not in (1, 3):
                raise ValueError(f"unsupported conductivity: {key}")
            materials[name] = {"conductivity_w_per_m_k": conductivity,
                               "heat_capacity_j_per_m3_k": positive(match[2]) * 1e18}
        elif kind == "layer":
            match = match_section(key, rf"height\s+({NUMBER})\s*;\s*material\s+({NAME})\s*;", full=True)
            definitions[key] = [{"thickness_um": positive(match[1]), "material": match[2], "powered": False}]
        elif kind == "die":
            pattern = rf"\s*(source|layer)\s+({NUMBER})\s+({NAME})\s*;"
            matches = list(re.finditer(pattern, body))
            if re.sub(pattern, "", body).strip() or sum(m[1] == "source" for m in matches) != 1:
                raise ValueError(f"unsupported die definition (requires exactly one source): {key}")
            definitions[key] = [{"thickness_um": positive(m[2]), "material": m[3], "powered": m[1] == "source"}
                                for m in matches]

    dimensions = match_section("dimensions", rf"chip length\s+({NUMBER})\s*,\s*width\s+({NUMBER})\s*;")
    cell = match_section("dimensions", rf"cell length\s+({NUMBER})\s*,\s*width\s+({NUMBER})\s*;")
    cooling = match_section("top heat sink", rf"heat transfer coefficient\s+({NUMBER})\s*;\s*"
                            rf"temperature\s+({NUMBER})\s*;", full=True)
    if "bottom heat sink" in sections:
        raise ValueError("bottom heat sink is not supported by this schematic reader")
    solver = match_section("solver", rf"transient step\s+({NUMBER})\s*,\s*slot\s+({NUMBER})\s*;")
    initial = match_section("solver", rf"initial temperature\s+({NUMBER})\s*;")
    layers, depth = [], 0.
    body = sections.get("stack", "")
    if not body.endswith(";"):
        raise ValueError("missing or incomplete stack elements")
    for statement in body[:-1].split(";"):
        match = re.fullmatch(rf"\s*(layer|die)\s+({NAME})\s+({NAME})(?:\s+floorplan\s+\"[^\"]+\")?(?:\s+discretization\s+(\d+)\s+(\d+))?\s*", statement)
        if match is None or (match[1] == "die") != ("floorplan" in statement):
            raise ValueError(f"unsupported stack element: {statement.strip()}")
        definition = definitions.get(f"{match[1]} {match[3]}")
        if definition is None or any(layer["material"] not in materials for layer in definition):
            raise ValueError(f"undefined layer or material: {statement.strip()}")
        if any(layer.get("die", layer["name"]) == match[2] for layer in layers):
            raise ValueError(f"duplicate stack element: {match[2]}")
        for i, layer in enumerate(definition):
            kind = "SOURCE" if layer["powered"] else "BEOL" if layer["material"].endswith("_BEOL") else "BULK"
            name = match[2] if len(definition) == 1 else f"{match[2]}_{kind}_{i}"
            physical = {"name": name, **layer, "top_depth_um": depth}
            if match[4] is not None:
                if match[1] != "layer" or min(int(match[4]), int(match[5])) <= 0:
                    raise ValueError("invalid passive-layer discretization")
                physical["discretization"] = [int(match[4]), int(match[5])]
            if match[1] == "die":
                physical["die"] = match[2]
            layers.append(physical)
            depth += layer["thickness_um"]
    return {"layers_top_to_bottom": layers, "total_thickness_um": depth, "materials": materials,
            "footprint_um": [positive(v) for v in dimensions.groups()],
            "cell_um": [positive(v) for v in cell.groups()],
            "non_uniform": bool(re.search(r"non-uniform\s+true\s*;", sections["dimensions"])),
            "heat_transfer_coefficient_w_per_m2_k": positive(cooling[1]) * 1e12,
            "sink_temperature_k": positive(cooling[2]), "initial_temperature_k": positive(initial[1]),
            "step_seconds": positive(solver[1]), "slot_seconds": positive(solver[2])}


def schematic_blocks(stack):
    blocks = []
    beol_dies = {layer.get("die") for layer in stack["layers_top_to_bottom"]
                 if layer["material"].endswith("_BEOL")}
    for layer in stack["layers_top_to_bottom"]:
        die = layer.get("die")
        beol = layer["material"].endswith("_BEOL")
        if (die and not beol and blocks and blocks[-1].get("die") == die
                and not blocks[-1]["material"].endswith("_BEOL")):
            blocks[-1]["thickness_um"] += layer["thickness_um"]
            blocks[-1]["powered"] |= layer["powered"]
            blocks[-1]["source_layers"].append(layer["name"])
            continue
        group = re.fullmatch(r"(TIM|PCB)(?:_\d+)?", layer["name"])
        merge = group[1] if group and not layer["powered"] else None
        label_key = layer["material"] if beol else die or layer["name"]
        label = merge or {"HEATSINK": "Heatsink", "TOP_DIE": "Silicon die", "BUMP": "Bumps",
                          "LOGIC_DIE": "Logic die", "DRAM_DIE": "DRAM die", "F2F_BOND": "Face-to-face bond",
                          "LOGIC_BEOL": "Logic BEOL", "DRAM_BEOL": "DRAM BEOL", "HYBRID_BOND": "Hybrid bond"}.get(
            label_key, label_key.replace("_", " "))
        if die and die in beol_dies and not beol:
            label = label.replace(" die", " silicon")
        if layer["name"] == "TOP_DIE" and layer["material"] != "SILICON":
            label = "Active die"
        if (merge and blocks and blocks[-1]["name"] == merge
                and blocks[-1]["material"] == layer["material"] and not blocks[-1]["powered"]):
            blocks[-1]["thickness_um"] += layer["thickness_um"]
            blocks[-1]["source_layers"].append(layer["name"])
        else:
            blocks.append({"name": label, "material": layer["material"], "powered": layer["powered"],
                           "thickness_um": layer["thickness_um"], "source_layers": [layer["name"]]})
            if die:
                blocks[-1]["die"] = die
    return blocks


def output_directory(path, sources):
    output = Path(path).resolve()
    if any(output == source.resolve() or source.resolve() in output.parents for source in sources):
        raise ValueError("output must be outside the source run/recording directory")
    output.mkdir(parents=True, exist_ok=True)
    return output


def plot_stack(stack_path, output):
    stack_path = Path(stack_path).resolve()
    stack = read_stack(stack_path)
    blocks = schematic_blocks(stack)
    output = output_directory(output, [stack_path.parent])
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/thermal-replay-analysis-mpl")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    colors = {"CU": "#DCA66C", "TIM": "#8ED0D2", "BUMP": "#BDA9D7", "HYBRID_BOND": "#BDA9D7",
              "LOGIC_BEOL": "#E4C18A", "DRAM_BEOL": "#E4C18A"}
    with plt.rc_context({"font.family": "DejaVu Sans", "pdf.fonttype": 42, "svg.fonttype": "none"}):
        fig = plt.figure(figsize=(7.2, max(4.5, len(blocks) * 1.36)))
        axis = fig.add_axes([.06, .06, .88, .88])
        axis.set(xlim=(-.02, 1.02), ylim=(-.04, len(blocks) + .04))
        axis.set_axis_off()
        for i, block in enumerate(blocks):
            y = len(blocks) - i - 1
            color = ("#A8C8E8" if block.get("die") == "DRAM_DIE" else "#EC9A78") if block["powered"] else colors.get(block["material"], "#D4E0EC")
            axis.add_patch(Rectangle((0, y), 1, 1, facecolor=color, edgecolor="white", linewidth=2))
            shift = .08 if y == 0 else 0
            axis.text(.5, y + .65 + shift, block["name"], ha="center", va="center",
                      fontsize=19, weight="bold", color="#1D2B3A")
            axis.text(.5, y + .32 + shift, f'{block["thickness_um"]:g} µm', ha="center", va="center",
                      fontsize=16, color="#1D2B3A")
        length, width = stack["footprint_um"]
        axis.text(.5, .13, f"Footprint: {length / 1000:.3f} × {width / 1000:.3f} mm²",
                  ha="center", va="center", fontsize=12.5, color="#1D2B3A")
        for extension in ("png", "pdf", "svg"):
            fig.savefig(output / f"layer_stack.{extension}", dpi=300, facecolor="white")
        plt.close(fig)
    metadata = {"source_stack": str(stack_path), "source_stack_sha256": digest(stack_path),
                **stack, "presentation": "equal-height schematic; source/bulk and consecutive TIM/PCB layers merged; BEOL separate",
                "blocks_top_to_bottom": blocks}
    write_json(output / "layer_stack.json", metadata)
    return metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", required=True, type=Path, help="Saved runtime ice.stk")
    parser.add_argument("--output", required=True, type=Path, help="Figure directory outside the source directory")
    args = parser.parse_args(argv)
    try:
        plot_stack(args.stack, args.output)
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, f"Stack plot failed: {error}\n")
    print(f"Saved layer_stack PNG/PDF/SVG and metadata in {args.output}")


if __name__ == "__main__":
    main()
