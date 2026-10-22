"""Explicit physical inputs for the SoftHier stacked logic/DRAM package.

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


def validate_material(section, location):
    key = "thermal_conductivity_w_m_k"
    conductivity = section.get(key)
    if isinstance(conductivity, list):
        if len(conductivity) != 3:
            raise ValueError(f"{location}.{key} must be a number or three x/y/z values")
        section[key] = [positive_number(v, f"{location}.{key}[{i}]") for i, v in enumerate(conductivity)]
    else:
        section[key] = positive_number(conductivity, f"{location}.{key}")
    key = "volumetric_heat_capacity_j_m3_k"
    section[key] = positive_number(section.get(key), f"{location}.{key}")


def validate_thermal_config(document):
    """Return normalized inputs, rejecting incomplete or impossible packages."""
    if not isinstance(document, dict):
        raise ValueError("thermal config must be an object")
    result = copy.deepcopy(document)
    if type(result.get("schema_version")) is not int or result["schema_version"] != 1:
        raise ValueError("thermal config schema_version must be 1")
    if not isinstance(result.get("provenance"), str) or not result["provenance"].strip():
        raise ValueError("thermal config provenance must describe the input source")
    stack_order = result.setdefault("stack_order", "dram_on_logic")
    if not isinstance(stack_order, str) or stack_order not in ("dram_on_logic", "logic_on_dram"):
        raise ValueError("thermal config stack_order must be 'dram_on_logic' or 'logic_on_dram'")
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
            beol_thickness = 0.0
            if "beol" in section:
                beol = section["beol"]
                if not isinstance(beol, dict):
                    raise ValueError(f"{name}.beol must be an object")
                beol["thickness_um"] = positive_number(beol.get("thickness_um"), f"{name}.beol.thickness_um")
                validate_material(beol, f"{name}.beol")
                beol_thickness = beol["thickness_um"]
            if section["source_thickness_um"] + beol_thickness >= section["thickness_um"]:
                raise ValueError(f"{name} source plus BEOL thickness must be less than total thickness_um")
        if name != "logic_die":
            validate_material(section, name)
    if result["face_to_face_bond"].get("technology", "effective") not in ("effective", "hybrid"):
        raise ValueError("face_to_face_bond.technology must be 'effective' or 'hybrid'")
    return result


def load_thermal_config(path):
    try:
        return validate_thermal_config(json.loads(Path(path).read_text()))
    except (OSError, ValueError) as exc:
        raise ValueError(f"thermal config {path}: {exc}") from exc


def memory_on_logic_stack(config, initial_temperature_k=300.0):
    """Construct either face-to-face die order; retain the legacy entry point."""
    config = validate_thermal_config(config)
    logic, dram, bond = (config[key] for key in ("logic_die", "dram_die", "face_to_face_bond"))
    hybrid = bond.get("technology") == "hybrid"
    bond_material = "HYBRID_BOND" if hybrid else "F2F"
    materials = {
        "SILICON": {"thermal_conductivity_w_m_k": 130.0, "volumetric_heat_capacity_j_m3_k": 1.61e6},
        "CU": {"thermal_conductivity_w_m_k": 385.0, "volumetric_heat_capacity_j_m3_k": 3.45e6},
        "TIM": {"thermal_conductivity_w_m_k": 5.0, "volumetric_heat_capacity_j_m3_k": 2.5e6},
        "BUMP": {"thermal_conductivity_w_m_k": [5.5, 5.5, 113.0], "volumetric_heat_capacity_j_m3_k": 1.845e6},
    }
    sections = [("DRAM", dram), (bond_material, bond)]
    for name, die in (("LOGIC", logic), ("DRAM", dram)):
        if "beol" in die:
            sections.append((f"{name}_BEOL", die["beol"]))
    for material, section in sections:
        materials[material] = {key: section[key] for key in (
            "thermal_conductivity_w_m_k", "volumetric_heat_capacity_j_m3_k")}
    layers = [{"id": f"PCB_{i}", "material": "SILICON", "thickness_um": 250.0} for i in (4, 3, 2, 1)]
    layers.append({"id": "BUMP", "material": "BUMP", "thickness_um": 50.0})
    ordered_dies = [("LOGIC", logic, "SILICON", "chip"), ("DRAM", dram, "DRAM", "dram")]
    if config["stack_order"] == "logic_on_dram":
        ordered_dies.reverse()
    dies = []
    for index, (name, section, material, geometry_root) in enumerate(ordered_dies):
        die_id = f"{name}_DIE"
        depth = section.get("beol", {}).get("thickness_um", 0.0)
        die_layers = [
            {"id": f"{name}_BULK", "material": material,
             "thickness_um": section["thickness_um"] - section["source_thickness_um"] - depth, "die": die_id},
            {"id": f"{name}_SOURCE", "material": material,
             "thickness_um": section["source_thickness_um"], "die": die_id, "source": True},
        ]
        if depth:
            die_layers.append({"id": f"{name}_BEOL", "material": f"{name}_BEOL",
                               "thickness_um": depth, "die": die_id})
        if index == 1:
            layers.append({"id": "HYBRID_BOND" if hybrid else "F2F_BOND",
                           "material": bond_material, "thickness_um": bond["thickness_um"]})
            # The upper die faces downward: BEOL/source/bulk from bottom to top.
            die_layers.reverse()
        layers.extend(die_layers)
        die = {"id": die_id, "geometry_root": geometry_root,
               "total_thickness_um": section["thickness_um"], "active_face": "top" if index == 0 else "bottom"}
        if depth:
            die["source_depth_um"] = depth
        dies.append(die)
    layers.extend({"id": f"TIM_{i}", "material": "TIM", "thickness_um": 7.5} for i in (4, 3, 2, 1))
    layers.append({"id": "HEATSINK", "material": "CU", "thickness_um": 50.0})
    return {
        "order": "bottom-to-top", "materials": materials,
        "dies": dies,
        "layers": layers,
        "cooling": {"heat_transfer_coefficient_w_m2_k": 15000.0, "temperature_k": initial_temperature_k},
    }
