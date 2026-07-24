#!/usr/bin/env python3
"""Generate the run-local configuration for the 3D-ICE power hook."""

import argparse
import json
import math
import sys
from pathlib import Path


INTERFACE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(INTERFACE_DIR))

from system_contract import load_contract, thermal_components  # noqa: E402


def positive_float(value):
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise argparse.ArgumentTypeError("value must be finite and positive")
    return parsed


def nonnegative_float(value):
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0.0:
        raise argparse.ArgumentTypeError(
            "value must be finite and non-negative"
        )
    return parsed


def parse_args():
    parser = argparse.ArgumentParser(
        description="Write a run-local 3D-ICE/GVSoC hook configuration."
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--system-config", required=True)
    parser.add_argument("--floorplan", required=True)
    parser.add_argument("--power-trace", required=True)
    parser.add_argument("--temperature-output", required=True)
    parser.add_argument("--temperature-history", required=True)
    parser.add_argument("--poll-seconds", type=positive_float, default=0.02)
    parser.add_argument(
        "--timeout-seconds", type=positive_float, default=180.0
    )
    parser.add_argument("--default-power-w", type=nonnegative_float)
    return parser.parse_args()


def main():
    args = parse_args()
    system_config = Path(args.system_config).resolve()
    document = load_contract(system_config)
    try:
        components = thermal_components(document)
    except KeyError as exc:
        raise SystemExit(
            "system contract lacks the required thermal_feedback section"
        ) from exc
    if not components:
        raise SystemExit(
            "system contract declares no thermal-feedback components"
        )

    output = Path(args.output).resolve()
    config = {
        "hook": {
            "name": "3dice-gvsoc-power-hook",
            "version": 1,
        },
        "system_config": str(system_config),
        "floorplan": str(Path(args.floorplan).resolve()),
        "power_trace": str(Path(args.power_trace).resolve()),
        "temperature_output": str(
            Path(args.temperature_output).resolve()
        ),
        "temperature_history": str(
            Path(args.temperature_history).resolve()
        ),
        "poll_seconds": args.poll_seconds,
        "timeout_seconds": args.timeout_seconds,
        "default_power_w": args.default_power_w,
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(config, stream, indent=2, sort_keys=True)
        stream.write("\n")
    temporary.replace(output)
    print(
        f"Generated power-hook configuration for {len(components)} "
        f"component(s): {output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
