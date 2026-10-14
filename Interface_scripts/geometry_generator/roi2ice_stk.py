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
# See the License for the specific language governing permissions and
# limitations under the License.
#

import argparse
import math
import os
import re


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTPUT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
DEFAULT_FLOORPLAN_FILE = os.path.join(DEFAULT_OUTPUT_DIR, "floorplan_nopower.flp")
DEFAULT_STK_FILE = os.path.join(DEFAULT_OUTPUT_DIR, "ice.stk")
# Cooling boundary: 15000 W/(m^2 K), expressed in W/(um^2 K) for 3D-ICE.
DEFAULT_HEAT_TRANSFER_COEFFICIENT = 1.5e-8


def positive_float(value):
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def format_number(value):
    return f"{value:.15g}"


def parse_floorplan_region(floorplan_file):
    with open(floorplan_file, "r", encoding="utf-8") as f:
        floorplan = f.read()

    entry_re = re.compile(
        r"^\s*[^:\n]+\s*:\s*"
        r"\n\s*position\s+([^,]+),\s*([^;]+);"
        r"\s*\n\s*dimension\s+([^,]+),\s*([^;]+);",
        re.MULTILINE,
    )

    min_x = None
    min_y = None
    max_x = None
    max_y = None

    for match in entry_re.finditer(floorplan):
        x = float(match.group(1))
        y = float(match.group(2))
        width = float(match.group(3))
        height = float(match.group(4))

        min_x = x if min_x is None else min(min_x, x)
        min_y = y if min_y is None else min(min_y, y)
        max_x = x + width if max_x is None else max(max_x, x + width)
        max_y = y + height if max_y is None else max(max_y, y + height)

    if max_x is None or max_y is None:
        raise RuntimeError(f"No floorplan elements found in {floorplan_file}")

    return min_x, min_y, max_x, max_y


def get_floorplan_path_for_stk(floorplan_file, stk_file):
    stk_dir = os.path.dirname(os.path.abspath(stk_file))
    rel_path = os.path.relpath(os.path.abspath(floorplan_file), stk_dir)
    if not rel_path.startswith("."):
        rel_path = f"./{rel_path}"
    return rel_path


def stk_template(
    chip_length,
    chip_width,
    floorplan_path,
    slot_seconds,
    step_seconds,
    initial_temperature_k,
):
    # Reference stack: copper, four TIM layers, active silicon, bumps, four PCB layers.
    # All layers share the chip footprint; only TOP_DIE receives power inputs.
    # The reference models the PCB layers using silicon's material properties.
    return f"""material SILICON :
   thermal conductivity     1.30e-4 ;
   volumetric heat capacity 1.61e-12 ;

material CU :
   thermal conductivity     3.85e-4 ;
   volumetric heat capacity 3.45e-12 ;

material TIM :
   thermal conductivity     5e-6 ;
   volumetric heat capacity 2.5e-12 ;

material BUMP :
   thermal conductivity     5.5e-6, 5.5e-6, 1.13e-4 ;
   volumetric heat capacity 1.845e-12 ;

top heat sink :
   heat transfer coefficient {format_number(DEFAULT_HEAT_TRANSFER_COEFFICIENT)} ;
   temperature               {format_number(initial_temperature_k)} ;

dimensions :
   chip length {format_number(chip_length)}, width {format_number(chip_width)} ;
   cell length  {format_number(min(50.0, chip_length))}, width  {format_number(min(50.0, chip_width))} ;
   non-uniform true;

layer HEATSINK_LAYER :
   height 50 ;
   material CU ;

layer TIM_LAYER :
   height 7.5 ;
   material TIM ;

layer BUMP_LAYER :
   height 50 ;
   material BUMP ;

layer PCB :
   height 250 ;
   material SILICON ;

die IC :
   source  50 SILICON ;

stack:
   layer   HEATSINK  HEATSINK_LAYER ;
   layer   TIM_1     TIM_LAYER ;
   layer   TIM_2     TIM_LAYER ;
   layer   TIM_3     TIM_LAYER ;
   layer   TIM_4     TIM_LAYER ;
   die     TOP_DIE   IC      floorplan "{floorplan_path}" ;
   layer   BUMP      BUMP_LAYER ;
   layer   PCB_1     PCB ;
   layer   PCB_2     PCB ;
   layer   PCB_3     PCB ;
   layer   PCB_4     PCB ;

solver:
   transient step {format_number(step_seconds)}, slot {format_number(slot_seconds)} ;
   initial temperature {format_number(initial_temperature_k)} ;
   numofcores 8 ;   

output:
     Tflp( TOP_DIE,   "output_top_die_flp_avg.txt",    average, slot ) ;
     Tmap( TOP_DIE,    "output_top_die.txt",                     slot ) ;

"""


def roi2ice_stk(
    floorplan_file,
    stk_file,
    slot_seconds,
    step_seconds,
    initial_temperature_k,
):
    min_x, min_y, max_x, max_y = parse_floorplan_region(floorplan_file)

    if min_x != 0 or min_y != 0:
        print(f"Warning: floorplan region starts at ({min_x}, {min_y}); using max range for chip size.")

    floorplan_path = get_floorplan_path_for_stk(floorplan_file, stk_file)
    os.makedirs(os.path.dirname(os.path.abspath(stk_file)), exist_ok=True)

    with open(stk_file, "w", encoding="utf-8") as f:
        f.write(
            stk_template(
                max_x,
                max_y,
                floorplan_path,
                slot_seconds,
                step_seconds,
                initial_temperature_k,
            )
        )


def main():
    parser = argparse.ArgumentParser(description="Generate 3D-ICE STK file from a floorplan file")
    parser.add_argument("floorplan_file", nargs="?", default=DEFAULT_FLOORPLAN_FILE, help="Path to floorplan file")
    parser.add_argument("stk_file", nargs="?", default=DEFAULT_STK_FILE, help="Output STK file")
    parser.add_argument(
        "--slot-seconds",
        type=positive_float,
        default=0.2,
        help="3D-ICE slot duration in seconds. This should match the provider power interval.",
    )
    parser.add_argument(
        "--step-seconds",
        type=positive_float,
        help="3D-ICE transient solver step in seconds. Defaults to slot-seconds / 10.",
    )
    parser.add_argument(
        "--initial-temperature-k",
        type=positive_float,
        default=300.0,
        help="Initial and heat-sink temperature in Kelvin.",
    )
    args = parser.parse_args()

    step_seconds = args.step_seconds
    if step_seconds is None:
        step_seconds = args.slot_seconds / 10.0

    roi2ice_stk(
        args.floorplan_file,
        args.stk_file,
        args.slot_seconds,
        step_seconds,
        args.initial_temperature_k,
    )


if __name__ == "__main__":
    main()
