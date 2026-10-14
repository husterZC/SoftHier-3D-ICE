#!/usr/bin/env python3
"""Load and validate the simulator-neutral 3D-ICE coupling contract.

The contract is the boundary between a workload simulator provider and the
generic geometry, trace adaptation, and thermal-simulation code.  Providers
may obtain their data from Python configuration objects, generated metadata,
or any other source; consumers only depend on the JSON representation defined
here.
"""

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List


CONTRACT_NAME = "3dice-cosim-system"
CONTRACT_VERSION = 1
SUPPORTED_CONTRACT_VERSIONS = (1, 2)
GEOMETRY_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class ContractError(ValueError):
    """Raised when a coupling contract is malformed or inconsistent."""


def _mapping(value: Any, location: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{location} must be an object")
    return value


def _list(value: Any, location: str) -> List[Any]:
    if not isinstance(value, list):
        raise ContractError(f"{location} must be an array")
    return value


def _nonempty_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{location} must be a non-empty string")
    return value


def _geometry_name(value: Any, location: str) -> str:
    name = _nonempty_string(value, location)
    if not GEOMETRY_NAME_RE.fullmatch(name) or "__" in name:
        raise ContractError(
            f"{location} must contain only letters, digits, '_', '-', or '.', "
            "and must not contain '__'"
        )
    return name


def _component_path(value: Any, location: str) -> str:
    path = _nonempty_string(value, location)
    if not path.startswith("/") or path == "/":
        raise ContractError(f"{location} must be an absolute component path")
    parts = path.split("/")[1:]
    if any(
        not part or part in {".", "..", "*", "**"}
        for part in parts
    ):
        raise ContractError(
            f"{location} must be exact and canonical, without empty segments, "
            "'.', '..', or wildcards"
        )
    return path


def _finite_number(value: Any, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{location} must be a number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ContractError(f"{location} must be finite")
    return parsed


def _pair(value: Any, location: str, *, positive: bool) -> List[float]:
    items = _list(value, location)
    if len(items) != 2:
        raise ContractError(f"{location} must contain exactly two numbers")
    result = [
        _finite_number(items[0], f"{location}[0]"),
        _finite_number(items[1], f"{location}[1]"),
    ]
    if positive and any(item <= 0.0 for item in result):
        raise ContractError(f"{location} values must be positive")
    return result


def _validate_geometry_node(node: Any, location: str) -> None:
    data = _mapping(node, location)
    node_type = _nonempty_string(data.get("type"), f"{location}.type")
    if node_type not in {"die", "comp"}:
        raise ContractError(f"{location}.type must be 'die' or 'comp'")

    _pair(data.get("shape"), f"{location}.shape", positive=True)
    _pair(data.get("offset"), f"{location}.offset", positive=False)
    children = _mapping(data.get("subs"), f"{location}.subs")
    for child_name, child in children.items():
        _geometry_name(child_name, f"{location}.subs key")
        _validate_geometry_node(child, f"{location}.subs.{child_name}")


def geometry_entry(geometry: Dict[str, Any], entry_name: str) -> Dict[str, Any]:
    """Resolve a slash-delimited floorplan element in a geometry tree."""

    parts = entry_name.split("/")
    if not parts or any(not part for part in parts):
        raise ContractError(f"invalid floorplan element name {entry_name!r}")

    current: Dict[str, Any] = geometry
    for index, part in enumerate(parts):
        if part not in current:
            raise ContractError(
                f"floorplan element {entry_name!r} is absent from geometry "
                f"at {'/'.join(parts[: index + 1])!r}"
            )
        node = _mapping(current[part], f"geometry entry {entry_name!r}")
        if index == len(parts) - 1:
            return node
        current = _mapping(node.get("subs"), f"geometry entry {entry_name!r}.subs")

    raise ContractError(f"invalid floorplan element name {entry_name!r}")


def geometry_rectangle(geometry, name):
    """Return absolute x, y, width, height for a geometry node."""
    current, x, y = geometry, 0.0, 0.0
    for part in name.split("/"):
        node = current[part]
        x += node["offset"][0]
        y += node["offset"][1]
        current = node["subs"]
    return x, y, node["shape"][0], node["shape"][1]


def _positive_number(value, location):
    parsed = _finite_number(value, location)
    if parsed <= 0:
        raise ContractError(f"{location} must be positive")
    return parsed


def _validate_thermal_stack(root):
    stack = _mapping(root.get("thermal_stack"), "thermal_stack")
    if stack.get("order") != "bottom-to-top":
        raise ContractError("thermal_stack.order must be 'bottom-to-top'")
    materials = _mapping(stack.get("materials"), "thermal_stack.materials")
    for name, material in materials.items():
        _geometry_name(name, "thermal_stack.materials key")
        material = _mapping(material, f"thermal_stack.materials.{name}")
        conductivity = material.get("thermal_conductivity_w_m_k")
        if isinstance(conductivity, list):
            if len(conductivity) != 3:
                raise ContractError(f"material {name} conductivity must contain x/y/z values")
            for value in conductivity:
                _positive_number(value, f"material {name} conductivity")
        else:
            _positive_number(conductivity, f"material {name} conductivity")
        _positive_number(material.get("volumetric_heat_capacity_j_m3_k"), f"material {name} heat capacity")
    dies = _list(stack.get("dies"), "thermal_stack.dies")
    if not dies:
        raise ContractError("thermal_stack.dies must not be empty")
    die_ids, geometry_roots, footprint = set(), set(), None
    for die in dies:
        die = _mapping(die, "thermal_stack.die")
        die_id = _geometry_name(die.get("id"), "thermal_stack.die.id")
        if die_id in die_ids:
            raise ContractError(f"duplicate thermal die {die_id}")
        die_ids.add(die_id)
        geometry_root = _nonempty_string(die.get("geometry_root"), "thermal_stack.die.geometry_root")
        if geometry_root in geometry_roots or geometry_root not in root["geometry"]:
            raise ContractError("thermal dies must map distinct known geometry roots")
        geometry_roots.add(geometry_root)
        node = root["geometry"][geometry_root]
        if node["type"] != "die" or any(node["offset"]):
            raise ContractError("thermal die geometry roots must be dies at the origin")
        if footprint is not None and footprint != node["shape"]:
            raise ContractError("thermal dies must share a footprint")
        footprint = node["shape"]
        _positive_number(die.get("total_thickness_um"), f"{die_id}.total_thickness_um")
        if die.get("active_face") not in {"top", "bottom"}:
            raise ContractError(f"{die_id}.active_face must be 'top' or 'bottom'")
    if geometry_roots != set(root["geometry"]):
        raise ContractError("every geometry root must belong to a thermal die")
    layers = _list(stack.get("layers"), "thermal_stack.layers")
    layer_ids = set()
    die_layers = {die_id: [] for die_id in die_ids}
    for index, layer in enumerate(layers):
        layer = _mapping(layer, f"thermal_stack.layers[{index}]")
        layer_id = _geometry_name(layer.get("id"), "thermal layer id")
        if layer_id in layer_ids or layer_id in die_ids:
            raise ContractError("thermal layer and die IDs must be unique")
        layer_ids.add(layer_id)
        if layer.get("material") not in materials:
            raise ContractError(f"{layer_id} references an unknown material")
        _positive_number(layer.get("thickness_um"), f"{layer_id}.thickness_um")
        if "source" in layer and not isinstance(layer["source"], bool):
            raise ContractError(f"{layer_id}.source must be boolean")
        if "die" in layer:
            if layer["die"] not in die_ids:
                raise ContractError(f"{layer_id} references an unknown die")
            die_layers[layer["die"]].append(index)
        elif layer.get("source"):
            raise ContractError("a thermal source layer must belong to a die")
    for die in dies:
        indices = die_layers[die["id"]]
        if not indices or indices != list(range(indices[0], indices[-1] + 1)):
            raise ContractError(f"{die['id']} physical layers must be present and contiguous")
        sources = [i for i in indices if layers[i].get("source")]
        if len(sources) != 1:
            raise ContractError(f"{die['id']} requires exactly one source layer")
        face_index = indices[-1] if die["active_face"] == "top" else indices[0]
        if sources[0] != face_index:
            raise ContractError(f"{die['id']} source layer does not match its active_face")
        if not math.isclose(sum(layers[i]["thickness_um"] for i in indices), die["total_thickness_um"], rel_tol=1e-10):
            raise ContractError(f"{die['id']} layers do not preserve total thickness")
    cooling = _mapping(stack.get("cooling"), "thermal_stack.cooling")
    _positive_number(cooling.get("heat_transfer_coefficient_w_m2_k"), "cooling heat transfer coefficient")
    _positive_number(cooling.get("temperature_k"), "cooling temperature_k")

    # Each die must tile its footprint without overlap or omitted regions.
    by_name = {element["name"]: element for element in root["floorplan"]["elements"]}
    for die in dies:
        elements = [item for item in by_name.values() if item.get("die") == die["id"]]
        if not elements:
            raise ContractError(f"{die['id']} has no floorplan regions")
        rectangles = []
        for element in elements:
            if element["name"].split("/")[0] != die["geometry_root"]:
                raise ContractError(f"floorplan {element['name']} belongs to the wrong die")
            rectangle = geometry_rectangle(root["geometry"], element["name"])
            x, y, width, height = rectangle
            tolerance = max(footprint) * 1e-9
            if x < -tolerance or y < -tolerance or x + width > footprint[0] + tolerance or y + height > footprint[1] + tolerance:
                raise ContractError(f"floorplan {element['name']} is outside its die")
            for ox, oy, ow, oh in rectangles:
                if min(x + width, ox + ow) - max(x, ox) > tolerance and min(y + height, oy + oh) - max(y, oy) > tolerance:
                    raise ContractError(f"overlapping floorplan regions on {die['id']}")
            rectangles.append(rectangle)
        if not math.isclose(sum(w * h for _, _, w, h in rectangles), footprint[0] * footprint[1], rel_tol=1e-9):
            raise ContractError(f"floorplan regions do not cover {die['id']}")
    if any(element.get("die") not in die_ids for element in by_name.values()):
        raise ContractError("each version 2 floorplan element requires a known die")
    for component in root.get("thermal_feedback", {}).get("components", []):
        if component.get("die") not in die_ids or any(by_name[name]["die"] != component["die"] for name in component["floorplan_elements"]):
            raise ContractError("thermal-feedback components must map their own die")


def validate_contract(document: Any) -> Dict[str, Any]:
    """Validate and return a coupling contract document."""

    root = _mapping(document, "contract document")
    contract = _mapping(root.get("contract"), "contract")
    name = _nonempty_string(contract.get("name"), "contract.name")
    version = contract.get("version")
    if name != CONTRACT_NAME:
        raise ContractError(
            f"contract.name must be {CONTRACT_NAME!r}, got {name!r}"
        )
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in SUPPORTED_CONTRACT_VERSIONS
    ):
        raise ContractError(
            f"contract.version must be one of {SUPPORTED_CONTRACT_VERSIONS}, got {version!r}"
        )

    producer = _mapping(root.get("producer"), "producer")
    _nonempty_string(producer.get("name"), "producer.name")

    geometry = _mapping(root.get("geometry"), "geometry")
    if not geometry:
        raise ContractError("geometry must contain at least one root element")
    for root_name, node in geometry.items():
        _geometry_name(root_name, "geometry root name")
        _validate_geometry_node(node, f"geometry.{root_name}")

    power_trace = _mapping(root.get("power_trace"), "power_trace")
    trace_format = _nonempty_string(power_trace.get("format"), "power_trace.format")
    if trace_format != "whitespace-float-rows":
        raise ContractError(
            "power_trace.format must be 'whitespace-float-rows'"
        )
    unit = _nonempty_string(power_trace.get("unit"), "power_trace.unit")
    if unit != "W":
        raise ContractError("power_trace.unit must be 'W'")
    columns = [
        _nonempty_string(item, f"power_trace.columns[{index}]")
        for index, item in enumerate(
            _list(power_trace.get("columns"), "power_trace.columns")
        )
    ]
    if not columns:
        raise ContractError("power_trace.columns must not be empty")
    if len(set(columns)) != len(columns):
        raise ContractError("power_trace.columns must be unique")
    column_set = set(columns)

    floorplan = _mapping(root.get("floorplan"), "floorplan")
    elements = _list(floorplan.get("elements"), "floorplan.elements")
    if not elements:
        raise ContractError("floorplan.elements must not be empty")

    element_names = set()
    referenced_columns = set()
    column_reference_counts = {column: 0 for column in columns}
    for index, item in enumerate(elements):
        location = f"floorplan.elements[{index}]"
        element = _mapping(item, location)
        element_name = _nonempty_string(element.get("name"), f"{location}.name")
        if "__" in element_name:
            raise ContractError(
                f"{location}.name must use '/' separators, not '__'"
            )
        if element_name in element_names:
            raise ContractError(f"duplicate floorplan element {element_name!r}")
        element_names.add(element_name)

        node = geometry_entry(geometry, element_name)
        if _mapping(node.get("subs"), f"geometry entry {element_name!r}.subs"):
            raise ContractError(
                f"floorplan element {element_name!r} must reference a geometry leaf"
            )

        power = _mapping(element.get("power"), f"{location}.power")
        has_column = "column" in power
        has_constant = "constant_w" in power
        if has_column == has_constant:
            raise ContractError(
                f"{location}.power must define exactly one of column or constant_w"
            )

        if has_column:
            column = _nonempty_string(power["column"], f"{location}.power.column")
            if column not in column_set:
                raise ContractError(
                    f"{location}.power.column {column!r} is not declared in "
                    "power_trace.columns"
                )
            referenced_columns.add(column)
            column_reference_counts[column] += 1
        else:
            constant = _finite_number(
                power["constant_w"], f"{location}.power.constant_w"
            )
            if constant < 0.0:
                raise ContractError(
                    f"{location}.power.constant_w must be non-negative"
                )

    unused_columns = column_set - referenced_columns
    if unused_columns:
        raise ContractError(
            "power_trace.columns contains unreferenced columns: "
            + ", ".join(sorted(unused_columns))
        )
    repeated_columns = {
        column
        for column, count in column_reference_counts.items()
        if count > 1
    }
    if repeated_columns:
        raise ContractError(
            "floorplan elements reference power columns more than once: "
            + ", ".join(sorted(repeated_columns))
        )

    thermal_feedback = root.get("thermal_feedback")
    if thermal_feedback is not None:
        thermal = _mapping(thermal_feedback, "thermal_feedback")
        temperature_unit = _nonempty_string(
            thermal.get("temperature_unit"),
            "thermal_feedback.temperature_unit",
        )
        if temperature_unit != "C":
            raise ContractError(
                "thermal_feedback.temperature_unit must be 'C'"
            )
        initial_temperature_c = _finite_number(
            thermal.get("initial_temperature_c"),
            "thermal_feedback.initial_temperature_c",
        )
        if initial_temperature_c <= -273.15:
            raise ContractError(
                "thermal_feedback.initial_temperature_c must be above absolute zero"
            )

        components = _list(
            thermal.get("components"), "thermal_feedback.components"
        )
        if not components:
            raise ContractError(
                "thermal_feedback.components must not be empty"
            )

        component_paths = set()
        mapped_columns = set()
        for index, item in enumerate(components):
            location = f"thermal_feedback.components[{index}]"
            component = _mapping(item, location)
            path = _component_path(component.get("path"), f"{location}.path")
            if path in component_paths:
                raise ContractError(
                    f"duplicate thermal-feedback component path {path!r}"
                )
            for existing in component_paths:
                if path.startswith(existing + "/") or existing.startswith(path + "/"):
                    raise ContractError(
                        "thermal-feedback component paths must not overlap "
                        f"hierarchically: {existing!r} and {path!r}"
                    )
            component_paths.add(path)

            power_column = _nonempty_string(
                component.get("power_column"), f"{location}.power_column"
            )
            if power_column not in column_set:
                raise ContractError(
                    f"{location}.power_column {power_column!r} is not declared "
                    "in power_trace.columns"
                )
            if power_column in mapped_columns:
                raise ContractError(
                    f"duplicate thermal-feedback power column {power_column!r}"
                )
            mapped_columns.add(power_column)

            aggregation = _nonempty_string(
                component.get("aggregation"), f"{location}.aggregation"
            )
            if aggregation != "area-weighted-average":
                raise ContractError(
                    f"{location}.aggregation must be 'area-weighted-average'"
                )

            mapped_elements = [
                _nonempty_string(value, f"{location}.floorplan_elements[{item_index}]")
                for item_index, value in enumerate(
                    _list(
                        component.get("floorplan_elements"),
                        f"{location}.floorplan_elements",
                    )
                )
            ]
            if not mapped_elements:
                raise ContractError(
                    f"{location}.floorplan_elements must not be empty"
                )
            if len(set(mapped_elements)) != len(mapped_elements):
                raise ContractError(
                    f"{location}.floorplan_elements must be unique"
                )
            unknown_elements = set(mapped_elements) - element_names
            if unknown_elements:
                raise ContractError(
                    f"{location}.floorplan_elements contains unknown elements: "
                    + ", ".join(sorted(unknown_elements))
                )

        missing_columns = column_set - mapped_columns
        if missing_columns:
            raise ContractError(
                "thermal_feedback.components does not map power columns: "
                + ", ".join(sorted(missing_columns))
            )

    if version == 2:
        _validate_thermal_stack(root)
    return root


def load_contract(path) -> Dict[str, Any]:
    contract_path = Path(path).resolve()
    try:
        with contract_path.open("r", encoding="utf-8") as stream:
            document = json.load(stream)
    except FileNotFoundError as exc:
        raise ContractError(f"missing system contract: {contract_path}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"{contract_path}: invalid JSON: {exc}") from exc

    try:
        return validate_contract(document)
    except ContractError as exc:
        raise ContractError(f"{contract_path}: {exc}") from exc


def floorplan_elements(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    return document["floorplan"]["elements"]


def thermal_die_ids(document):
    """Powered dies in the native solver's bottom-to-top input order."""
    if document["contract"]["version"] == 1:
        return ["TOP_DIE"]
    return [layer["die"] for layer in document["thermal_stack"]["layers"] if layer.get("source")]


def power_order_elements(document):
    """Floorplan input order, including passive residual regions."""
    elements = floorplan_elements(document)
    if document["contract"]["version"] == 1:
        return elements
    return [element for die_id in thermal_die_ids(document) for element in elements if element["die"] == die_id]


def power_columns(document: Dict[str, Any]) -> List[str]:
    return document["power_trace"]["columns"]


def thermal_components(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    return document["thermal_feedback"]["components"]


def initial_temperature_c(document: Dict[str, Any]) -> float:
    return float(document["thermal_feedback"]["initial_temperature_c"])


def flattened_floorplan_name(name: str) -> str:
    return name.replace("/", "__")


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a simulator/3D-ICE system contract."
    )
    parser.add_argument("contract", help="System contract JSON file.")
    args = parser.parse_args()

    document = load_contract(args.contract)
    print(
        f"Valid {CONTRACT_NAME} v{document['contract']['version']}: "
        f"{len(power_columns(document))} power columns, "
        f"{len(floorplan_elements(document))} floorplan elements"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
