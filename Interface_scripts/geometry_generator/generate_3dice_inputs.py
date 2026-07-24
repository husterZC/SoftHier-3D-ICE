#!/usr/bin/env python3
"""Generate 3D-ICE inputs from a simulator-neutral system contract."""

import argparse
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
INTERFACE_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(INTERFACE_DIR))

from system_contract import load_contract  # noqa: E402


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
        description="Generate geometry, floorplan, and stack files for 3D-ICE."
    )
    parser.add_argument(
        "--system-config",
        required=True,
        help="Simulator-neutral system contract JSON file.",
    )
    parser.add_argument("--geo", required=True, help="Output geometry JSON path.")
    parser.add_argument("--floorplan", required=True, help="Output floorplan path.")
    parser.add_argument("--stk", required=True, help="Output stack file path.")
    parser.add_argument(
        "--power-interval-ps",
        "--pwr-interval-ps",
        dest="power_interval_ps",
        type=positive_int,
        required=True,
        help="Raw power capture interval in picoseconds.",
    )
    parser.add_argument(
        "--slot-seconds",
        type=positive_float,
        help="3D-ICE slot duration. Defaults to power-interval-ps * 1e-12.",
    )
    parser.add_argument(
        "--step-seconds",
        type=positive_float,
        help="Transient step duration. Defaults to slot-seconds / 10.",
    )
    parser.add_argument(
        "--target-top-die-cells",
        type=positive_int,
        default=256 * 256,
        help="Approximate non-uniform TOP_DIE cell count.",
    )
    return parser.parse_args()


def run(command):
    subprocess.run(command, check=True)


def main() -> int:
    args = parse_args()
    system_config = Path(args.system_config).resolve()
    geo = Path(args.geo).resolve()
    floorplan = Path(args.floorplan).resolve()
    stk = Path(args.stk).resolve()

    if not system_config.is_file():
        raise SystemExit(f"missing system contract: {system_config}")

    document = load_contract(system_config)
    initial_temperature_c = float(
        document.get("thermal_feedback", {}).get(
            "initial_temperature_c", 26.85
        )
    )
    initial_temperature_k = initial_temperature_c + 273.15

    slot_seconds = args.slot_seconds
    if slot_seconds is None:
        slot_seconds = args.power_interval_ps * 1e-12
    step_seconds = args.step_seconds
    if step_seconds is None:
        step_seconds = slot_seconds / 10.0

    geo.parent.mkdir(parents=True, exist_ok=True)
    floorplan.parent.mkdir(parents=True, exist_ok=True)
    stk.parent.mkdir(parents=True, exist_ok=True)

    run([sys.executable, str(GEOGEN), str(system_config), str(geo)])
    run(
        [
            sys.executable,
            str(ROI2ICE_FLOORPLAN),
            str(system_config),
            str(geo),
            str(floorplan),
            "--target-top-die-cells",
            str(args.target_top_die_cells),
        ]
    )
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
            "--initial-temperature-k",
            f"{initial_temperature_k:.15g}",
        ]
    )

    print(f"Generated geometry: {geo}")
    print(f"Generated floorplan: {floorplan}")
    print(f"Generated stk: {stk}")
    print(f"Aligned 3D-ICE slot: {slot_seconds:.15g} s")
    print(f"3D-ICE transient step: {step_seconds:.15g} s")
    print(f"3D-ICE initial temperature: {initial_temperature_k:.15g} K")
    print(f"Target TOP_DIE cells: {args.target_top_die_cells}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
