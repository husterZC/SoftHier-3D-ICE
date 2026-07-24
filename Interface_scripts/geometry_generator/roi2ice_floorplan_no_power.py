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

"""Generate a non-uniform 3D-ICE floorplan from the system contract."""

import argparse
import json
import math
import os
import sys
from pathlib import Path


INTERFACE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(INTERFACE_DIR))

from system_contract import floorplan_elements, load_contract  # noqa: E402


DEFAULT_TARGET_TOP_DIE_CELLS = 256 * 256


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def contract_roi(document):
    return [entry["name"] for entry in floorplan_elements(document)]


def ice_template(name, position, dimension, discretization):
    return f"""{name} :

    position {position[0]}, {position[1]};
    dimension {dimension[0]}, {dimension[1]};
    discretization  {discretization[0]}, {discretization[1]} ;
"""


def get_roi_entry(geometry, entry):
    path = entry.split("/")
    geo = geometry
    position = [0, 0]

    for index, name in enumerate(path):
        if index == len(path) - 1:
            leaf = geo[name]
            dimension = leaf["shape"]
            position[0] += leaf["offset"][0]
            position[1] += leaf["offset"][1]
            return {
                "name": entry.replace("/", "__"),
                "position": position,
                "dimension": dimension,
            }

        node = geo[name]
        position[0] += node["offset"][0]
        position[1] += node["offset"][1]
        geo = node["subs"]

    raise RuntimeError(f"Invalid ROI entry: {entry}")


def discretization_for_pitch(dimension, pitch):
    length, width = dimension
    return max(1, round(length / pitch)), max(1, round(width / pitch))


def total_discretized_cells(entries, pitch):
    total = 0
    for entry in entries:
        rows, columns = discretization_for_pitch(entry["dimension"], pitch)
        total += rows * columns
    return total


def choose_target_pitch(entries, target_cells):
    total_area = sum(
        entry["dimension"][0] * entry["dimension"][1] for entry in entries
    )
    if total_area <= 0.0:
        raise RuntimeError("ROI floorplan area must be positive")

    base_pitch = math.sqrt(total_area / target_cells)
    lower = base_pitch * 0.5
    upper = base_pitch * 2.0
    best_pitch = base_pitch
    best_total = total_discretized_cells(entries, base_pitch)
    best_error = abs(best_total - target_cells)

    # Rounded element counts form plateaus.  A deterministic dense scan keeps
    # results stable across Python versions and matches the previous flow.
    samples = 10001
    for index in range(samples):
        pitch = lower + (upper - lower) * index / (samples - 1)
        total = total_discretized_cells(entries, pitch)
        error = abs(total - target_cells)
        if error < best_error or (error == best_error and pitch < best_pitch):
            best_pitch = pitch
            best_total = total
            best_error = error
            if best_error == 0:
                break

    return best_pitch, best_total


def assign_discretization(entries, target_cells):
    pitch, actual_cells = choose_target_pitch(entries, target_cells)
    for entry in entries:
        entry["discretization"] = discretization_for_pitch(
            entry["dimension"], pitch
        )
    return actual_cells, pitch


def roi2ice(geometry, roi, output_file, target_top_die_cells):
    entries = [get_roi_entry(geometry, entry) for entry in roi]
    actual_cells, pitch = assign_discretization(entries, target_top_die_cells)

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as stream:
        for entry in entries:
            stream.write(
                ice_template(
                    entry["name"],
                    entry["position"],
                    entry["dimension"],
                    entry["discretization"],
                )
            )
            stream.write("\n")

    print(
        "Generated floorplan TOP_DIE discretization: "
        f"target {target_top_die_cells}, actual {actual_cells}, pitch {pitch:.6g}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a 3D-ICE floorplan from a system contract."
    )
    parser.add_argument("system_config", help="System contract JSON file.")
    parser.add_argument("geo_file", help="Geometry JSON generated from the contract.")
    parser.add_argument("output_file", help="Output 3D-ICE floorplan file.")
    parser.add_argument(
        "--target-top-die-cells",
        type=positive_int,
        default=DEFAULT_TARGET_TOP_DIE_CELLS,
        help="Approximate total non-uniform TOP_DIE cells.",
    )
    args = parser.parse_args()

    document = load_contract(args.system_config)
    with Path(args.geo_file).resolve().open("r", encoding="utf-8") as stream:
        geometry = json.load(stream)

    roi2ice(
        geometry,
        contract_roi(document),
        str(Path(args.output_file).resolve()),
        args.target_top_die_cells,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
