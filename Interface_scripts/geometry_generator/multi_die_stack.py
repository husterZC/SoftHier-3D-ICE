"""Generate a 3D-ICE stack from the version 2 physical-layer contract."""

from pathlib import Path
import math

from roi2ice_stk import format_number, get_floorplan_path_for_stk


def write_stack(document, floorplans, output, slot_seconds, step_seconds, grid_pitch_um=None,
                map_output="slot"):
    """Write physical layers top-to-bottom as required by 3D-ICE grammar.

    The contract and power inputs use bottom-to-top order. Die layer definitions
    also use top-to-bottom order, with exactly one explicitly positioned source.
    """
    stack = document["thermal_stack"]
    if grid_pitch_um is not None and (not math.isfinite(grid_pitch_um) or grid_pitch_um <= 0):
        raise ValueError("grid pitch must be finite and positive")
    if map_output not in ("slot", "final"):
        raise ValueError("map output must be slot or final")
    parts = []
    for name, material in stack["materials"].items():
        conductivity = material["thermal_conductivity_w_m_k"]
        if not isinstance(conductivity, list):
            conductivity = [conductivity]
        conductivity = ", ".join(format_number(value * 1e-6) for value in conductivity)
        capacity = format_number(material["volumetric_heat_capacity_j_m3_k"] * 1e-18)
        parts.append(f"material {name} :\n   thermal conductivity {conductivity} ;\n   volumetric heat capacity {capacity} ;\n")
    cooling = stack["cooling"]
    coefficient = format_number(cooling["heat_transfer_coefficient_w_m2_k"] * 1e-12)
    temperature = format_number(cooling["temperature_k"])
    parts.append(f"top heat sink :\n   heat transfer coefficient {coefficient} ;\n   temperature {temperature} ;\n")
    first_die = stack["dies"][0]
    length, width = document["geometry"][first_die["geometry_root"]]["shape"]
    # The native parser truncates chip/pitch to an integer for passive layers.
    # A pitch larger than a small test die would silently omit those layers.
    cell_length, cell_width = min(50.0, length), min(50.0, width)
    discretization = ""
    if grid_pitch_um is not None:
        nx, ny = math.ceil(length / grid_pitch_um), math.ceil(width / grid_pitch_um)
        cell_length, cell_width = length / nx, width / ny
        discretization = f" discretization {nx} {ny}"
    parts.append(f"dimensions :\n   chip length {format_number(length)}, width {format_number(width)} ;\n   cell length {format_number(cell_length)}, width {format_number(cell_width)} ;\n   non-uniform true;\n")
    for layer in stack["layers"]:
        if "die" not in layer:
            parts.append(f"layer {layer['id']}_LAYER :\n   height {format_number(layer['thickness_um'])} ;\n   material {layer['material']} ;\n")
    for die in stack["dies"]:
        definition = [f"die {die['id']}_IC :"]
        for layer in reversed(stack["layers"]):
            if layer.get("die") == die["id"]:
                keyword = "source" if layer.get("source") else "layer"
                definition.append(f"   {keyword} {format_number(layer['thickness_um'])} {layer['material']} ;")
        parts.append("\n".join(definition) + "\n")
    elements, emitted_dies = ["stack:"], set()
    for layer in reversed(stack["layers"]):
        die = layer.get("die")
        if die:
            if die not in emitted_dies:
                path = get_floorplan_path_for_stk(floorplans[die], output)
                elements.append(f'   die {die} {die}_IC floorplan "{path}" ;')
                emitted_dies.add(die)
        else:
            elements.append(f"   layer {layer['id']} {layer['id']}_LAYER{discretization} ;")
    parts.append("\n".join(elements) + "\n")
    initial = document.get("thermal_feedback", {}).get("initial_temperature_c", 26.85) + 273.15
    # SuperLU_MT's parallel factorization produced sub-ambient temperatures for
    # positive-power, full-size two-die cases. Match thermal replay's conservative
    # one-thread setting until parallel native factorization is validated.
    parts.append(f"solver:\n   transient step {format_number(step_seconds)}, slot {format_number(slot_seconds)} ;\n   initial temperature {format_number(initial)} ;\n   numofcores 1 ;\n")
    outputs = ["output:"]
    for die in stack["dies"]:
        name = die["id"].lower()
        outputs.extend([
            f'   Tflp( {die["id"]}, "output_{name}_flp_avg.txt", average, slot ) ;',
            f'   Tmap( {die["id"]}, "output_{name}.txt", {map_output} ) ;',
        ])
    parts.append("\n".join(outputs) + "\n")
    Path(output).write_text("\n".join(parts), encoding="utf-8")
