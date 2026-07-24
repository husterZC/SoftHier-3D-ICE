#!/usr/bin/env python3
#
# Copyright (C) 2025 ETH Zurich and University of Bologna
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#

"""Extract exact physical geometry from a simulator-neutral system contract."""

import argparse
import json
import sys
from pathlib import Path


INTERFACE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(INTERFACE_DIR))

from system_contract import load_contract  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract 3D-ICE geometry from a system contract."
    )
    parser.add_argument("system_config", help="System contract JSON file.")
    parser.add_argument("geo_file", help="Output geometry JSON file.")
    args = parser.parse_args()

    document = load_contract(args.system_config)
    geo_file = Path(args.geo_file).resolve()
    geo_file.parent.mkdir(parents=True, exist_ok=True)
    with geo_file.open("w", encoding="utf-8") as stream:
        json.dump(document["geometry"], stream, indent=4)
        stream.write("\n")

    print(f"Generated geometry from system contract: {geo_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
