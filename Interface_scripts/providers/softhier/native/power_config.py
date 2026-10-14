"""Validate explicit HBM2 core electrical profiles (SI units throughout)."""
import json
import math
from pathlib import Path

STATES = ("precharged", "active", "power_down_active", "power_down_precharged", "self_refresh")
COMMANDS = ("ACT", "PRE", "RD", "WR", "REFAB", "REFPB")


def validate_power_config(config: dict, *, allow_synthetic: bool = False) -> dict:
    if not isinstance(config, dict):
        raise ValueError("DRAM power config must be an object")
    if config.get("schema_version") != 1 or isinstance(config.get("schema_version"), bool):
        raise ValueError("DRAM power schema_version must be 1")
    if config.get("memory_type") != "HBM2" or config.get("scope") != "per_pseudo_channel":
        raise ValueError("DRAM power requires HBM2 and scope=per_pseudo_channel")
    if config.get("synthetic", False) and not allow_synthetic:
        raise ValueError("synthetic DRAM electrical fixtures cannot be used for experiments")
    if not isinstance(config.get("provenance"), str) or not config["provenance"].strip():
        raise ValueError("DRAM power provenance must be a nonempty string")

    def number(value, name, *, positive=False):
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number")
        if value <= 0 if positive else value < 0:
            raise ValueError(f"{name} must be {'positive' if positive else 'nonnegative'}")

    number(config.get("characterization_temperature_k"), "characterization_temperature_k", positive=True)
    rails = config.get("rails")
    if not isinstance(rails, list) or not rails:
        raise ValueError("DRAM power rails must be a nonempty list")
    names = set()
    for rail in rails:
        if not isinstance(rail, dict):
            raise ValueError("each DRAM rail must be an object")
        name = rail.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("DRAM rail names must be nonempty and unique")
        names.add(name)
        number(rail.get("voltage_v"), f"{name}.voltage_v", positive=True)
        for field, keys in (("background_current_a", STATES), ("command_charge_c", COMMANDS)):
            values = rail.get(field)
            if not isinstance(values, dict) or set(values) != set(keys):
                raise ValueError(f"{name}.{field} requires exactly {', '.join(keys)}")
            for key in keys:
                number(values[key], f"{name}.{field}.{key}")
    return config


def load_power_config(path, *, allow_synthetic: bool = False) -> dict:
    with Path(path).open(encoding="utf-8") as stream:
        return validate_power_config(json.load(stream), allow_synthetic=allow_synthetic)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    load_power_config(args.config)
    print(f"Valid HBM2 core electrical profile: {args.config}")
