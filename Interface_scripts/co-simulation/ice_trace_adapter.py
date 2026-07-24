#!/usr/bin/env python3
"""Adapt provider power rows to the element order in a 3D-ICE floorplan.

The simulator-neutral system contract declares the meaning of each input
column and the power source for every floorplan element.  No simulator module
or architecture class is imported here.
"""

import argparse
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional


INTERFACE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(INTERFACE_DIR))

from system_contract import (  # noqa: E402
    flattened_floorplan_name,
    floorplan_elements,
    load_contract,
    power_columns,
)


FLOORPLAN_ENTRY_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*:\s*$")


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0.0:
        raise argparse.ArgumentTypeError("value must be finite and non-negative")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Follow a raw provider power trace and write 3D-ICE slots."
    )
    parser.add_argument(
        "--system-config",
        required=True,
        help="Simulator-neutral system contract JSON file.",
    )
    parser.add_argument(
        "--floorplan",
        required=True,
        help="Generated floorplan file whose element order must be followed.",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Raw provider power trace; each complete line is one slot.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="3D-ICE power trace to create and append to.",
    )
    parser.add_argument(
        "--done-file",
        required=True,
        help="When this file appears, append an all-minus-one slot and exit.",
    )
    parser.add_argument(
        "--default-power-w",
        "--others-power",
        dest="default_power_w",
        type=nonnegative_float,
        default=None,
        help=(
            "Override every constant power value declared by the contract. "
            "--others-power is retained as a compatibility alias."
        ),
    )
    parser.add_argument("--poll", type=float, default=0.2, help="Polling interval.")
    parser.add_argument(
        "--preserve-output",
        action="store_true",
        help="Append instead of truncating the output on startup.",
    )
    return parser.parse_args()


def parse_floorplan_entries(floorplan_path: Path) -> List[str]:
    entries: List[str] = []
    with floorplan_path.open("r", encoding="utf-8") as stream:
        for line in stream:
            match = FLOORPLAN_ENTRY_RE.match(line)
            if match is not None:
                entries.append(match.group(1))

    if not entries:
        raise RuntimeError(f"No floorplan elements found in {floorplan_path}")
    return entries


def build_power_sources(document: dict) -> Dict[str, dict]:
    return {
        flattened_floorplan_name(element["name"]): element["power"]
        for element in floorplan_elements(document)
    }


def validate_floorplan_entries(entries: List[str], power_sources: Dict[str, dict]) -> None:
    entry_set = set(entries)
    if len(entry_set) != len(entries):
        raise RuntimeError("3D-ICE floorplan contains duplicate element names")

    missing_contract_entries = entry_set - set(power_sources)
    stale_contract_entries = set(power_sources) - entry_set
    if missing_contract_entries or stale_contract_entries:
        details = []
        if missing_contract_entries:
            details.append(
                "floorplan-only: " + ", ".join(sorted(missing_contract_entries))
            )
        if stale_contract_entries:
            details.append(
                "contract-only: " + ", ".join(sorted(stale_contract_entries))
            )
        raise RuntimeError("floorplan/contract element mismatch (" + "; ".join(details) + ")")


def parse_power_values(line: str, raw_path: Path, line_number: int) -> List[float]:
    fields = line.strip().split()
    if not fields:
        return []
    try:
        values = [float(field) for field in fields]
    except ValueError as exc:
        raise RuntimeError(
            f"{raw_path}:{line_number}: non-numeric power value in {line!r}"
        ) from exc
    if any(not math.isfinite(value) or value < 0.0 for value in values):
        raise RuntimeError(
            f"{raw_path}:{line_number}: power values must be finite and non-negative"
        )
    return values


def raw_power_map(
    values: List[float], columns: List[str], raw_path: Path, line_number: int
) -> Dict[str, float]:
    if len(values) != len(columns):
        raise RuntimeError(
            f"{raw_path}:{line_number}: expected {len(columns)} raw values "
            f"from the system contract, got {len(values)}"
        )
    return dict(zip(columns, values))


def adapt_slot(
    values: List[float],
    columns: List[str],
    floorplan_entries_in_order: Iterable[str],
    power_sources: Dict[str, dict],
    default_power_override: Optional[float],
    raw_path: Path,
    line_number: int,
) -> List[float]:
    mapped = raw_power_map(values, columns, raw_path, line_number)
    output_values = []

    for entry in floorplan_entries_in_order:
        power = power_sources[entry]
        if "column" in power:
            output_values.append(mapped[power["column"]])
        elif default_power_override is not None:
            output_values.append(default_power_override)
        else:
            output_values.append(float(power["constant_w"]))

    return output_values


def write_slot(stream, values: Iterable[float]) -> None:
    stream.write(" ".join(f"{value:.15g}" for value in values))
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())


def follow_raw_trace(args: argparse.Namespace) -> int:
    document = load_contract(args.system_config)
    columns = power_columns(document)
    power_sources = build_power_sources(document)

    floorplan_path = Path(args.floorplan).resolve()
    raw_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    done_path = Path(args.done_file).resolve()
    if raw_path == output_path:
        raise RuntimeError("--input and --output must be different files")

    floorplan_entries_in_order = parse_floorplan_entries(floorplan_path)
    validate_floorplan_entries(floorplan_entries_in_order, power_sources)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    output_mode = "a" if args.preserve_output else "w"
    line_number = 0
    slots_written = 0

    with output_path.open(output_mode, encoding="utf-8") as output_stream:
        while not raw_path.exists():
            if done_path.exists():
                write_slot(output_stream, [-1.0] * len(floorplan_entries_in_order))
                print(
                    "Done file appeared before raw trace existed; wrote termination slot.",
                    flush=True,
                )
                return 0
            time.sleep(args.poll)

        with raw_path.open("r", encoding="utf-8") as raw_stream:
            while True:
                offset = raw_stream.tell()
                line = raw_stream.readline()
                if not line:
                    raw_stream.seek(offset)
                    if done_path.exists():
                        write_slot(
                            output_stream,
                            [-1.0] * len(floorplan_entries_in_order),
                        )
                        print(
                            f"Wrote termination slot after {slots_written} adapted slot(s).",
                            flush=True,
                        )
                        return 0
                    time.sleep(args.poll)
                    continue

                if not line.endswith("\n"):
                    raw_stream.seek(offset)
                    if done_path.exists():
                        raise RuntimeError(
                            f"{raw_path}:{line_number + 1}: provider exited with "
                            "an incomplete final power row"
                        )
                    time.sleep(args.poll)
                    continue

                line_number += 1
                values = parse_power_values(line, raw_path, line_number)
                if not values:
                    continue
                adapted_values = adapt_slot(
                    values,
                    columns,
                    floorplan_entries_in_order,
                    power_sources,
                    args.default_power_w,
                    raw_path,
                    line_number,
                )
                write_slot(output_stream, adapted_values)
                slots_written += 1


def main() -> int:
    args = parse_args()
    try:
        return follow_raw_trace(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"ice_trace_adapter.py: error: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
