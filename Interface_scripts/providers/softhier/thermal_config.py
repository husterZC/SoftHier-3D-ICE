"""Explicit physical inputs for the SoftHier memory-on-logic package.

Input material properties use SI units; geometry uses micrometres.  No DRAM
or bonding characterization is inferred from the electrical memory standard.
"""

import copy
import json
import math
from pathlib import Path


DEFAULT_THERMAL_CONFIG = Path(__file__).resolve().parent / "configs/dram3d_thermal.json"


def positive_number(value, location):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{location} must be a positive finite number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{location} must be a positive finite number")
    return float(value)


def validate_thermal_config(document):
    """Return normalized inputs, rejecting incomplete or impossible packages."""
    if not isinstance(document, dict):
        raise ValueError("thermal config must be an object")
    result = copy.deepcopy(document)
    if type(result.get("schema_version")) is not int or result["schema_version"] != 1:
        raise ValueError("thermal config schema_version must be 1")
    if not isinstance(result.get("provenance"), str) or not result["provenance"].strip():
        raise ValueError("thermal config provenance must describe the input source")
    for name in ("logic_die", "dram_die", "face_to_face_bond"):
        section = result.get(name)
        if not isinstance(section, dict):
            raise ValueError(f"thermal config requires {name}")
        if name == "logic_die":
            section.setdefault("thickness_um", 50.0)
        section["thickness_um"] = positive_number(section.get("thickness_um"), f"{name}.thickness_um")
        if name.endswith("die"):
            section["source_thickness_um"] = positive_number(
                section.get("source_thickness_um"), f"{name}.source_thickness_um")
            if section["source_thickness_um"] >= section["thickness_um"]:
                raise ValueError(f"{name}.source_thickness_um must be less than total thickness_um")
        if name != "logic_die":
            key = "thermal_conductivity_w_m_k"
            conductivity = section.get(key)
            if isinstance(conductivity, list):
                if len(conductivity) != 3:
                    raise ValueError(f"{name}.{key} must be a number or three x/y/z values")
                section[key] = [positive_number(v, f"{name}.{key}[{i}]") for i, v in enumerate(conductivity)]
            else:
                section[key] = positive_number(conductivity, f"{name}.{key}")
            key = "volumetric_heat_capacity_j_m3_k"
            section[key] = positive_number(section.get(key), f"{name}.{key}")
    return result


def load_thermal_config(path):
    try:
        return validate_thermal_config(json.loads(Path(path).read_text()))
    except (OSError, ValueError) as exc:
        raise ValueError(f"thermal config {path}: {exc}") from exc


def memory_on_logic_stack(config, initial_temperature_k=300.0):
    """Construct a bottom-to-top physical stack, preserving package defaults."""
    config = validate_thermal_config(config)
    materials = {
        "SILICON": {"thermal_conductivity_w_m_k": 130.0, "volumetric_heat_capacity_j_m3_k": 1.61e6},
        "CU": {"thermal_conductivity_w_m_k": 385.0, "volumetric_heat_capacity_j_m3_k": 3.45e6},
        "TIM": {"thermal_conductivity_w_m_k": 5.0, "volumetric_heat_capacity_j_m3_k": 2.5e6},
        "BUMP": {"thermal_conductivity_w_m_k": [5.5, 5.5, 113.0], "volumetric_heat_capacity_j_m3_k": 1.845e6},
    }
    for material, section in (("DRAM", "dram_die"), ("F2F", "face_to_face_bond")):
        materials[material] = {key: config[section][key] for key in (
            "thermal_conductivity_w_m_k", "volumetric_heat_capacity_j_m3_k")}
    layers = [{"id": f"PCB_{i}", "material": "SILICON", "thickness_um": 250.0} for i in (4, 3, 2, 1)]
    layers.append({"id": "BUMP", "material": "BUMP", "thickness_um": 50.0})
    logic, dram, bond = (config[key] for key in ("logic_die", "dram_die", "face_to_face_bond"))
    layers.extend([
        {"id": "LOGIC_BULK", "material": "SILICON", "thickness_um": logic["thickness_um"] - logic["source_thickness_um"], "die": "LOGIC_DIE"},
        {"id": "LOGIC_SOURCE", "material": "SILICON", "thickness_um": logic["source_thickness_um"], "die": "LOGIC_DIE", "source": True},
        {"id": "F2F_BOND", "material": "F2F", "thickness_um": bond["thickness_um"]},
        {"id": "DRAM_SOURCE", "material": "DRAM", "thickness_um": dram["source_thickness_um"], "die": "DRAM_DIE", "source": True},
        {"id": "DRAM_BULK", "material": "DRAM", "thickness_um": dram["thickness_um"] - dram["source_thickness_um"], "die": "DRAM_DIE"},
    ])
    layers.extend({"id": f"TIM_{i}", "material": "TIM", "thickness_um": 7.5} for i in (4, 3, 2, 1))
    layers.append({"id": "HEATSINK", "material": "CU", "thickness_um": 50.0})
    return {
        "order": "bottom-to-top", "materials": materials,
        "dies": [
            {"id": "LOGIC_DIE", "geometry_root": "chip", "total_thickness_um": logic["thickness_um"], "active_face": "top"},
            {"id": "DRAM_DIE", "geometry_root": "dram", "total_thickness_um": dram["thickness_um"], "active_face": "bottom"},
        ],
        "layers": layers,
        "cooling": {"heat_transfer_coefficient_w_m2_k": 15000.0, "temperature_k": initial_temperature_k},
    }
