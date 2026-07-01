#!/usr/bin/env python3
"""Generate 3D-ICE geometry, floorplan, and stack files from a SoftHier config."""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
GEOGEN = SCRIPT_DIR / "geogen.py"
ROI2ICE_FLOORPLAN = SCRIPT_DIR / "roi2ice_floorplan_no_power.py"
ROI2ICE_STK = SCRIPT_DIR / "roi2ice_stk.py"


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate geo.json, 3D-ICE floorplan, and 3D-ICE stk files."
    )
    parser.add_argument("--arch", required=True, help="SoftHier architecture Python file.")
    parser.add_argument("--geo", required=True, help="Output geometry JSON path.")
    parser.add_argument("--floorplan", required=True, help="Output 3D-ICE floorplan path.")
    parser.add_argument("--stk", required=True, help="Output 3D-ICE stack file path.")
    parser.add_argument(
        "--pwr-interval-ps",
        type=positive_int,
        required=True,
        help="SoftHier power capture interval in picoseconds.",
    )
    parser.add_argument(
        "--slot-seconds",
        type=positive_float,
        help="3D-ICE slot duration in seconds. Defaults to pwr-interval-ps * 1e-12.",
    )
    parser.add_argument(
        "--step-seconds",
        type=positive_float,
        help="3D-ICE transient solver step in seconds. Defaults to slot-seconds / 10.",
    )
    parser.add_argument(
        "--target-top-die-cells",
        type=positive_int,
        default=256 * 256,
        help="Approximate total non-uniform TOP_DIE cells requested in the generated floorplan.",
    )
    return parser.parse_args()


def run(command):
    subprocess.run(command, check=True)


def main() -> int:
    args = parse_args()

    arch = Path(args.arch).resolve()
    geo = Path(args.geo).resolve()
    floorplan = Path(args.floorplan).resolve()
    stk = Path(args.stk).resolve()

    if not arch.is_file():
        raise SystemExit(f"missing architecture file: {arch}")

    slot_seconds = args.slot_seconds
    if slot_seconds is None:
        slot_seconds = args.pwr_interval_ps * 1e-12

    step_seconds = args.step_seconds
    if step_seconds is None:
        step_seconds = slot_seconds / 10.0

    geo.parent.mkdir(parents=True, exist_ok=True)
    floorplan.parent.mkdir(parents=True, exist_ok=True)
    stk.parent.mkdir(parents=True, exist_ok=True)

    run([sys.executable, str(GEOGEN), str(arch), str(geo)])

    run(
        [
            sys.executable,
            str(ROI2ICE_FLOORPLAN),
            str(arch),
            str(geo),
            str(floorplan.parent),
            "--target-top-die-cells",
            str(args.target_top_die_cells),
        ]
    )
    generated_floorplan = floorplan.parent / "floorplan_nopower.flp"
    if generated_floorplan.resolve() != floorplan:
        shutil.move(str(generated_floorplan), str(floorplan))

    run(
        [
            sys.executable,
            str(ROI2ICE_STK),
            str(floorplan),
            str(stk),
            "--slot-seconds",
            f"{slot_seconds:.15g}",
            "--step-seconds",
            f"{step_seconds:.15g}",
        ]
    )

    print(f"Generated geometry: {geo}")
    print(f"Generated floorplan: {floorplan}")
    print(f"Generated stk: {stk}")
    print(f"Aligned 3D-ICE slot: {slot_seconds:.15g} s")
    print(f"3D-ICE transient step: {step_seconds:.15g} s")
    print(f"Target TOP_DIE cells: {args.target_top_die_cells}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
