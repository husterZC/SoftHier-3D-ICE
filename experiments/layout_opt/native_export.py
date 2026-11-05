"""Normalize floating-point zero at native floorplan boundaries after D4 mapping."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from experiments.layout_opt.transforms import local, transform_contract, write_json
from Interface_scripts.system_contract import validate_contract


def native_contract(contract, plan, digest=None):
    result = transform_contract(contract, plan, digest)
    for cluster in result["geometry"]["chip"]["subs"].values():
        tolerance = max(cluster["shape"])*1e-12
        for region in cluster["subs"].values():
            for axis in (0, 1):
                value = region["offset"][axis]
                if -tolerance <= value < 0:
                    region["offset"][axis] = 0.
    return validate_contract(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system-config", type=Path, required=True)
    parser.add_argument("--layout", type=Path, required=True)
    args = parser.parse_args()
    payload = local(args.layout).read_bytes()
    result = native_contract(json.loads(local(args.system_config).read_text()),
        json.loads(payload), hashlib.sha256(payload).hexdigest())
    write_json(args.system_config, result)


if __name__ == "__main__":
    main()
