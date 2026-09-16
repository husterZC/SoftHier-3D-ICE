#!/usr/bin/env python3
"""Translate a SoftHier architecture into the neutral coupling contract."""

import argparse
import copy
import importlib.util
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


INTERFACE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(INTERFACE_DIR))

from system_contract import (  # noqa: E402
    CONTRACT_NAME,
    CONTRACT_VERSION,
    validate_contract,
)
from providers.softhier import floorplans  # noqa: E402


PNR_UTILIZATION = 0.66
SRAM_BITCELL_UM2 = {
    "22nm": 0.100,
    "12nm": 0.060,
    "7nm": 0.027,
    "5nm": 0.021,
}


def power_models():
    """Load the provider's single coefficient/area source without importing GVSoC."""
    softhier = Path(os.environ.get("SOFTHIER_DIR", INTERFACE_DIR.parent / "SoftHier"))
    path = softhier / "pulp/pulp/chips/soft_hier_old/power_models/__init__.py"
    spec = importlib.util.spec_from_file_location("softhier_power_models", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def component_inventory(architecture: Any) -> List[Dict[str, Any]]:
    models = power_models()
    node = str(getattr(architecture, "tech_node", "5nm"))
    cell_area = models.technology_spec(node)["cell_um2_per_kge"] / PNR_UTILIZATION
    cores = required_int(architecture, "num_core_per_cluster", minimum=1)
    attached = list(getattr(architecture, "spatz_attaced_core_list", []))
    if len(set(attached)) != len(attached) or any(i not in range(cores) for i in attached):
        raise ValueError("Spatz attachment indices must be unique valid core indices")
    if getattr(architecture, "core_model", "fast") != "fast":
        raise ValueError("expanded thermal mappings currently require core_model='fast'")
    inventory = []

    def add(name, kind, path, parameters=None, area=None):
        parameters = parameters or {}
        if area is None:
            area = models.logic_area_kge(kind, **parameters) * cell_area
        if not math.isfinite(area) or area <= 0:
            raise ValueError(f"invalid area for {name}: {area}")
        inventory.append(dict(name=name, kind=kind, simulator_path=path,
                              parameters=parameters, area_um2=area))

    red_kge = 100 + required_int(architecture, "redmule_ce_height", minimum=1) * required_int(architecture, "redmule_ce_width", minimum=1) * 8.59
    add("redmule", "light_redmule", "/chip/{cluster}/redmule", area=red_kge * cell_area)
    size = required_int(architecture, "cluster_tcdm_size", minimum=1)
    add("tcdm", "memory", "/chip/{cluster}/tcdm", {"size_bytes": size}, size * 8 * SRAM_BITCELL_UM2[node])
    for core in range(cores):
        add(f"pe{core}", "core", f"/chip/{{cluster}}/pe{core}/scalar_power")
        if core in attached:
            ports = required_int(architecture, "spatz_num_vlsu_port", minimum=1)
            vlen_bits = ports * int(getattr(architecture, "spatz_vlsu_port_width", 32))
            add(f"spatz{core}", "spatz", f"/chip/{{cluster}}/pe{core}/ara", {
                "function_units": required_int(architecture, "spatz_num_function_unit", minimum=1),
                "vrf_bytes": 32 * vlen_bits // 8, "vlsu_ports": ports})
    width = required_int(architecture, "noc_link_width", minimum=1)
    dma_names = [f"idma_{i}" for i in range(cores)] if getattr(architecture, "multi_idma_enable", 0) else ["idma"]
    for name in dma_names:
        add(name, "idma", f"/chip/{{cluster}}/{name}", {
            "outstanding": required_int(architecture, "idma_outstand_txn", minimum=1), "data_width_bits": width})
    for network, bits in (("data_noc", width), ("sync_bus", 32)):
        for part in ("router", "ni"):
            add(f"{network}_{part}", "floonoc", f"/chip/{network}/{part}_{{router_x}}_{{router_y}}",
                {"part": part, "data_width_bits": bits})
    for name, field in (("instr_mem", "instruction_mem_size"), ("stack_mem", "cluster_stack_size")):
        size = required_int(architecture, field, minimum=1)
        add(name, "memory", f"/chip/{{cluster}}/{name}", {"size_bytes": size}, size * 8 * SRAM_BITCELL_UM2[node])
    add("transpose_engine", "transpose", "/chip/{cluster}/transpose_engine", {
        # Architecture bank width is in bits; ClusterArch passes bytes to the
        # transpose model. Keep the physical/power estimate in the same units.
        "buffer_bytes": required_int(architecture, "cluster_tcdm_bank_width", minimum=8) / 8 * required_int(architecture, "cluster_tcdm_bank_nb", minimum=1)})
    return inventory


def import_architecture(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import architecture file: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    architecture_class = getattr(module, "FlexClusterArch", None)
    if architecture_class is None:
        raise RuntimeError(f"{path} does not define FlexClusterArch")
    return architecture_class()


def required_int(architecture: Any, name: str, *, minimum: int = 0) -> int:
    if not hasattr(architecture, name):
        raise RuntimeError(f"SoftHier architecture is missing {name}")
    value = int(getattr(architecture, name))
    if value < minimum:
        raise RuntimeError(
            f"SoftHier architecture field {name} must be >= {minimum}, got {value}"
        )
    return value


def build_cluster_geometry(
    architecture: Any,
    floorplan_rule: str = floorplans.DEFAULT_RULE,
) -> Tuple[Dict[str, Any], List[float]]:
    return floorplans.build_cluster(component_inventory(architecture), floorplan_rule)


def build_contract(
    architecture: Any,
    source_config: Path,
    default_power_w: float,
    power_profile: str = "constant",
    floorplan_rule: str = floorplans.DEFAULT_RULE,
) -> dict:
    cluster_columns = required_int(architecture, "num_cluster_x", minimum=1)
    cluster_rows = required_int(architecture, "num_cluster_y", minimum=1)
    cluster_count = cluster_columns * cluster_rows
    cluster_components, cluster_shape = build_cluster_geometry(architecture, floorplan_rule)
    inventory = component_inventory(architecture)
    resolved_components = []

    clusters = {}
    floorplan_elements = []
    power_columns = []
    thermal_components = []
    for y in range(cluster_rows):
        for x in range(cluster_columns):
            cluster_index = x + y * cluster_columns
            cluster_name = f"cluster_{cluster_index}"
            clusters[cluster_name] = {
                "type": "comp",
                "shape": list(cluster_shape),
                "offset": [x * cluster_shape[0], y * cluster_shape[1]],
                "subs": copy.deepcopy(cluster_components),
            }

            others = f"chip/{cluster_name}/others"
            for item in inventory:
                column = f"chip/{cluster_name}/{item['name']}"
                path = item["simulator_path"].format(cluster=cluster_name, router_x=x + 1, router_y=y + 1)
                power_columns.append(column)
                thermal_components.append({"path": path, "power_column": column,
                    "floorplan_elements": [column], "aggregation": "area-weighted-average"})
                floorplan_elements.append({"name": column, "power": {"column": column}})
                resolved_components.append(dict(item, simulator_path=path, power_column=column))
            floorplan_elements.append({"name": others, "power": {"constant_w": default_power_w}})

    geometry = {
        "chip": {
            "type": "die",
            "shape": [
                cluster_columns * cluster_shape[0],
                cluster_rows * cluster_shape[1],
            ],
            "offset": [0.0, 0.0],
            "subs": clusters,
        }
    }

    contract = {
        "contract": {
            "name": CONTRACT_NAME,
            "version": CONTRACT_VERSION,
        },
        "producer": {
            "name": "softhier",
            "source_config": str(source_config),
        },
        "metadata": {
            "cluster_grid": {
                "columns": cluster_columns,
                "rows": cluster_rows,
                "count": cluster_count,
            },
            "core_count_per_cluster": required_int(
                architecture, "num_core_per_cluster", minimum=1
            ),
            "technology_node": str(getattr(architecture, "tech_node", "5nm")),
            "power_model_profile": power_profile,
            "component_power_model_version": 2,
            "power_voltage_v": power_models().technology_spec(str(getattr(architecture, "tech_node", "5nm")))["nominal_voltage_v"],
            "power_frequency_hz": 1_000_000_000,
            "power_estimate_scale": float(getattr(architecture, "power_estimate_scale", 1.0)),
            "components": resolved_components,
            "power_model_specs": {name: power_models().model_spec(name) for name in
                ("technology", "core", "spatz", "idma", "floonoc", "transpose", "memory", "light_redmule")},
            "floorplan_rule": floorplan_rule,
            "floorplan_method": floorplans.get_rule(floorplan_rule).DESCRIPTION,
            "floorplan_residual_area_fraction": floorplans.RESIDUAL_AREA_FRACTION,
            "unmodeled": ["HBM/PHY", "local interconnect/register logic (others residual)", "global clock distribution beyond component clock budgets"],
        },
        "geometry": geometry,
        "floorplan": {
            "elements": floorplan_elements,
        },
        "power_trace": {
            "format": "whitespace-float-rows",
            "unit": "W",
            "columns": power_columns,
        },
        "thermal_feedback": {
            "temperature_unit": "C",
            "initial_temperature_c": 26.85,
            "components": thermal_components,
        },
    }
    return validate_contract(contract)


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0.0:
        raise argparse.ArgumentTypeError("value must be finite and non-negative")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export a SoftHier architecture as a 3D-ICE system contract."
    )
    parser.add_argument("--arch", required=True, help="SoftHier architecture file.")
    parser.add_argument("--output", required=True, help="Output contract JSON file.")
    parser.add_argument("--floorplan", choices=tuple(floorplans.RULES), default=floorplans.DEFAULT_RULE,
                        help="Cluster placement rule; does not change power coefficients or component areas.")
    parser.add_argument("--core-model", choices=("fast", "accurate"),
                        help="Apply the same core-model override as the simulator.")
    parser.add_argument(
        "--power-profile",
        choices=("constant", "temperature_aware"),
        default="constant",
        help="SoftHier component leakage profile recorded in contract metadata.",
    )
    parser.add_argument(
        "--default-power-w",
        type=nonnegative_float,
        default=0.0,
        help="Constant power for geometry elements absent from the raw trace.",
    )
    args = parser.parse_args()

    source_config = Path(args.arch).resolve()
    output = Path(args.output).resolve()
    if not source_config.is_file():
        raise SystemExit(f"missing SoftHier architecture file: {source_config}")

    architecture = import_architecture(source_config)
    if args.core_model is not None:
        architecture.core_model = args.core_model
    contract = build_contract(
        architecture,
        source_config,
        args.default_power_w,
        args.power_profile,
        args.floorplan,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as stream:
        json.dump(contract, stream, indent=2)
        stream.write("\n")

    print(f"Exported SoftHier system contract: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
