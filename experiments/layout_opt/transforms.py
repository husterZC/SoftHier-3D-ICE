"""The eight rigid symmetries of square_bands, applied to complete clusters."""

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from Interface_scripts.system_contract import validate_contract
from Interface_scripts.providers.softhier.floorplans.common import validate_layout

# x' = a*x + b*y + tx*side; y' = c*x + d*y + ty*side.
# Rotations are counterclockwise. MX/MY negate x/y; MD/MA mirror diagonally.
SYMMETRIES = {
    "R0": (1, 0, 0, 1, 0, 0),
    "R90": (0, -1, 1, 0, 1, 0),
    "R180": (-1, 0, 0, -1, 1, 1),
    "R270": (0, 1, -1, 0, 0, 1),
    "MX": (-1, 0, 0, 1, 1, 0),
    "MY": (1, 0, 0, -1, 0, 1),
    "MD": (0, 1, 1, 0, 0, 0),
    "MA": (0, -1, -1, 0, 1, 1),
}


def local(path):
    path = Path(path).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError(f"path must be inside this repository: {path}")
    return path


def write_json(path, value):
    path = local(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def plan_for(transforms, columns=4, rows=4, rationale=""):
    if len(transforms) != columns * rows:
        raise ValueError("one transform is required per cluster")
    return dict(schema_version=1, base_rule="square_bands", columns=columns, rows=rows,
        transforms={f"cluster_{i}": value for i, value in enumerate(transforms)},
        rationale=rationale)


def validate_plan(plan, grid):
    if type(plan.get("schema_version")) is not int or plan["schema_version"] != 1:
        raise ValueError("layout schema_version must be 1")
    if plan.get("base_rule") != "square_bands":
        raise ValueError("only square_bands symmetries are permitted")
    for field in ("columns", "rows"):
        if type(plan.get(field)) is not int or plan[field] != grid[field]:
            raise ValueError(f"layout {field} differs from the physical cluster grid")
    expected = {f"cluster_{i}" for i in range(grid["columns"] * grid["rows"])}
    mapping = plan.get("transforms")
    if not isinstance(mapping, dict) or set(mapping) != expected:
        raise ValueError("layout must explicitly specify every cluster exactly once")
    if any(not isinstance(t, str) or t not in SYMMETRIES for t in mapping.values()):
        raise ValueError("layout contains an unsupported square symmetry")
    return mapping


def transform_regions(regions, shape, symmetry):
    if symmetry not in SYMMETRIES:
        raise ValueError("unsupported square symmetry")
    if len(shape) != 2 or not math.isclose(*shape, rel_tol=1e-12) or shape[0] <= 0:
        raise ValueError("cluster must be square")
    result = copy.deepcopy(regions)
    if symmetry == "R0":
        return result
    a, b, c, d, tx, ty = SYMMETRIES[symmetry]
    side = shape[0]
    for region in result.values():
        x, y = region["offset"]
        w, h = region["shape"]
        region["offset"] = [a*x + b*y + tx*side + min(a*w, 0) + min(b*h, 0),
                            c*x + d*y + ty*side + min(c*w, 0) + min(d*h, 0)]
        region["shape"] = [abs(a)*w + abs(b)*h, abs(c)*w + abs(d)*h]
    inventory = [dict(name=name, area_um2=math.prod(region["shape"]))
                 for name, region in regions.items()]
    validate_layout(inventory, result, shape)
    return result


def transform_contract(contract, plan, plan_sha256=None):
    if contract["metadata"]["floorplan_rule"] != "square_bands":
        raise ValueError("the input contract must use square_bands")
    if "layout_optimization" in contract["metadata"]:
        raise ValueError("refusing to transform an already transformed contract")
    mapping = validate_plan(plan, contract["metadata"]["cluster_grid"])
    result = copy.deepcopy(contract)
    for name, cluster in result["geometry"]["chip"]["subs"].items():
        cluster["subs"] = transform_regions(cluster["subs"], cluster["shape"], mapping[name])
    result["metadata"]["layout_optimization"] = dict(schema_version=1,
        base_rule="square_bands", transforms=dict(mapping), layout_sha256=plan_sha256,
        convention="cluster index x + columns*y; origin bottom left; rotations counterclockwise")
    # The thermal stack, DRAM placement, power columns, paths, component areas,
    # cluster positions and feedback mappings are unchanged.
    return validate_contract(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system-config", type=Path, required=True)
    parser.add_argument("--layout", type=Path, required=True)
    args = parser.parse_args()
    payload = local(args.layout).read_bytes()
    result = transform_contract(json.loads(local(args.system_config).read_text()),
        json.loads(payload), hashlib.sha256(payload).hexdigest())
    write_json(args.system_config, result)


if __name__ == "__main__":
    main()
