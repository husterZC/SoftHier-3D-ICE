#!/usr/bin/env python3
"""Translate a SoftHier architecture into the neutral coupling contract."""

import argparse
import copy
import importlib.util
import json
import math
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


PNR_UTILIZATION = 0.66
KGE_TO_UM2 = {
    "22nm": 200,
    "12nm": 120,
    "7nm": 60,
    "5nm": 30,
}
SRAM_BITCELL_UM2 = {
    "22nm": 0.100,
    "12nm": 0.060,
    "7nm": 0.027,
    "5nm": 0.021,
}


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
) -> Tuple[Dict[str, Any], List[float]]:
    tech_node = str(getattr(architecture, "tech_node", "5nm"))
    if tech_node not in KGE_TO_UM2:
        raise RuntimeError(
            f"unsupported SoftHier technology node {tech_node!r}; "
            f"choose one of {', '.join(KGE_TO_UM2)}"
        )

    redmule_ce_height = required_int(
        architecture, "redmule_ce_height", minimum=1
    )
    redmule_ce_width = required_int(architecture, "redmule_ce_width", minimum=1)
    redmule_kge = 100 + redmule_ce_height * redmule_ce_width * 8.59
    redmule_area = redmule_kge * KGE_TO_UM2[tech_node] / PNR_UTILIZATION
    redmule_dimension = math.sqrt(redmule_area)

    core_count = required_int(architecture, "num_core_per_cluster", minimum=1)
    spatz_function_units = required_int(
        architecture, "spatz_num_function_unit", minimum=0
    )
    attached_spatz = list(
        getattr(architecture, "spatz_attaced_core_list", [])
    )
    idma_outstanding = required_int(
        architecture, "idma_outstand_txn", minimum=0
    )
    noc_link_width = required_int(architecture, "noc_link_width", minimum=1)

    snitch_kge = 25 + 126
    spatz_kge = 169 + 46 + spatz_function_units * 142
    idma_kge = 7 + idma_outstanding * 6.5 + noc_link_width * 1.3 / 32
    noc_router_kge = 28 + 168 * noc_link_width / 512
    others_kge = (
        snitch_kge * core_count
        + spatz_kge * len(attached_spatz)
        + idma_kge
        + noc_router_kge
    )
    others_area = others_kge * KGE_TO_UM2[tech_node] / PNR_UTILIZATION
    others_height = others_area / redmule_dimension

    tcdm_bytes = required_int(architecture, "cluster_tcdm_size", minimum=1)
    tcdm_area = tcdm_bytes * 8 * SRAM_BITCELL_UM2[tech_node]
    cluster_height = redmule_dimension + others_height
    tcdm_width = tcdm_area / cluster_height

    components = {
        "redmule": {
            "type": "comp",
            "shape": [redmule_dimension, redmule_dimension],
            "offset": [0.0, 0.0],
            "subs": {},
        },
        "others": {
            "type": "comp",
            "shape": [redmule_dimension, others_height],
            "offset": [0.0, redmule_dimension],
            "subs": {},
        },
        "tcdm": {
            "type": "comp",
            "shape": [tcdm_width, cluster_height],
            "offset": [redmule_dimension, 0.0],
            "subs": {},
        },
    }
    return components, [redmule_dimension + tcdm_width, cluster_height]


def build_contract(
    architecture: Any, source_config: Path, default_power_w: float
) -> dict:
    cluster_columns = required_int(architecture, "num_cluster_x", minimum=1)
    cluster_rows = required_int(architecture, "num_cluster_y", minimum=1)
    cluster_count = cluster_columns * cluster_rows
    cluster_components, cluster_shape = build_cluster_geometry(architecture)

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

            redmule = f"chip/{cluster_name}/redmule"
            others = f"chip/{cluster_name}/others"
            tcdm = f"chip/{cluster_name}/tcdm"
            power_columns.extend([redmule, tcdm])
            thermal_components.extend(
                [
                    {
                        "path": f"/chip/{cluster_name}/redmule",
                        "power_column": redmule,
                        "floorplan_elements": [redmule],
                        "aggregation": "area-weighted-average",
                    },
                    {
                        "path": f"/chip/{cluster_name}/tcdm",
                        "power_column": tcdm,
                        "floorplan_elements": [tcdm],
                        "aggregation": "area-weighted-average",
                    },
                ]
            )
            floorplan_elements.extend(
                [
                    {"name": redmule, "power": {"column": redmule}},
                    {
                        "name": others,
                        "power": {"constant_w": default_power_w},
                    },
                    {"name": tcdm, "power": {"column": tcdm}},
                ]
            )

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
    contract = build_contract(architecture, source_config, args.default_power_w)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as stream:
        json.dump(contract, stream, indent=2)
        stream.write("\n")

    print(f"Exported SoftHier system contract: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
