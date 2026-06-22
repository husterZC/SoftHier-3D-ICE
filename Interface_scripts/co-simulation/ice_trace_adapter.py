#!/usr/bin/env python3
"""Adapt SoftHier power slots to 3D-ICE floorplan order.

SoftHier currently emits power slots in this per-cluster order:

    redmule, tcdm

The generated 3D-ICE floorplan currently contains this per-cluster order:

    redmule, others, tcdm

This adapter follows the raw SoftHier trace, inserts a configurable value for
the missing "others" region, and writes complete 3D-ICE slots. When the done
file appears, it appends an all-minus-one sentinel slot for 3D-ICE-Client's
--until-minus-one mode.
"""

import argparse
import importlib.util
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List


FLOORPLAN_ENTRY_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*:\s*$")
SOFTHIER_FLOORPLAN_RE = re.compile(
    r"^chip__cluster_(?P<cluster>[0-9]+)__(?P<region>redmule|others|tcdm)$"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Follow a SoftHier raw power trace and write a 3D-ICE power trace."
    )
    parser.add_argument("--arch", required=True, help="SoftHier architecture Python file.")
    parser.add_argument(
        "--floorplan",
        required=True,
        help="Generated 3D-ICE floorplan file that defines element order.",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Raw SoftHier power trace. Each complete line is one SoftHier slot.",
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
        "--others-power",
        type=float,
        default=0.0,
        help="Power value used for floorplan 'others' regions not present in SoftHier raw trace.",
    )
    parser.add_argument("--poll", type=float, default=0.2, help="Polling interval in seconds.")
    parser.add_argument(
        "--preserve-output",
        action="store_true",
        help="Append to output instead of truncating it on adapter startup.",
    )
    return parser.parse_args()


def import_architecture(arch_path: Path):
    spec = importlib.util.spec_from_file_location(arch_path.stem, arch_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import architecture file: {arch_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if not hasattr(module, "FlexClusterArch"):
        raise RuntimeError(f"{arch_path} does not define FlexClusterArch")

    return module.FlexClusterArch()


def cluster_count_from_arch(arch) -> int:
    try:
        return int(arch.num_cluster_x) * int(arch.num_cluster_y)
    except AttributeError as exc:
        raise RuntimeError("Architecture is missing num_cluster_x or num_cluster_y") from exc


def parse_floorplan_entries(floorplan_path: Path) -> List[str]:
    entries = []  # type: List[str]

    with floorplan_path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            match = FLOORPLAN_ENTRY_RE.match(line)
            if match is None:
                continue

            name = match.group(1)
            if name in {"material", "dimensions", "layer", "die", "stack", "solver", "output"}:
                continue

            entries.append(name)

    if not entries:
        raise RuntimeError(f"No floorplan elements found in {floorplan_path}")

    return entries


def parse_power_values(line: str, raw_path: Path, line_number: int) -> List[float]:
    fields = line.strip().split()
    if not fields:
        return []

    try:
        return [float(field) for field in fields]
    except ValueError as exc:
        raise RuntimeError(f"{raw_path}:{line_number}: non-numeric power value in {line!r}") from exc


def raw_power_map(
    values: List[float], cluster_count: int, raw_path: Path, line_number: int
) -> Dict[str, float]:
    expected = cluster_count * 2
    if len(values) != expected:
        raise RuntimeError(
            f"{raw_path}:{line_number}: expected {expected} raw values "
            f"(redmule,tcdm for {cluster_count} clusters), got {len(values)}"
        )

    mapped = {}  # type: Dict[str, float]
    for cluster_index in range(cluster_count):
        raw_index = cluster_index * 2
        mapped[f"chip__cluster_{cluster_index}__redmule"] = values[raw_index]
        mapped[f"chip__cluster_{cluster_index}__tcdm"] = values[raw_index + 1]

    return mapped


def adapt_slot(
    values: List[float],
    floorplan_entries: Iterable[str],
    cluster_count: int,
    others_power: float,
    raw_path: Path,
    line_number: int,
) -> List[float]:
    mapped = raw_power_map(values, cluster_count, raw_path, line_number)
    output_values = []  # type: List[float]

    for entry in floorplan_entries:
        match = SOFTHIER_FLOORPLAN_RE.match(entry)
        if match is None:
            raise RuntimeError(
                f"Unsupported floorplan element {entry!r}; "
                "expected chip__cluster_N__redmule/others/tcdm"
            )

        cluster_index = int(match.group("cluster"))
        region = match.group("region")

        if cluster_index >= cluster_count:
            raise RuntimeError(
                f"Floorplan element {entry!r} references cluster {cluster_index}, "
                f"but architecture has {cluster_count} clusters"
            )

        if region == "others":
            output_values.append(others_power)
        else:
            output_values.append(mapped[entry])

    return output_values


def write_slot(stream, values: Iterable[float]) -> None:
    stream.write(" ".join(f"{value:.15g}" for value in values))
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())


def follow_raw_trace(args: argparse.Namespace) -> int:
    arch_path = Path(args.arch).resolve()
    floorplan_path = Path(args.floorplan).resolve()
    raw_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    done_path = Path(args.done_file).resolve()

    if raw_path == output_path:
        raise RuntimeError("--input and --output must be different files")

    arch = import_architecture(arch_path)
    cluster_count = cluster_count_from_arch(arch)
    floorplan_entries = parse_floorplan_entries(floorplan_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    output_mode = "a" if args.preserve_output else "w"
    line_number = 0
    slots_written = 0
    terminated = False

    with output_path.open(output_mode, encoding="utf-8") as output_stream:
        while not raw_path.exists():
            if done_path.exists():
                write_slot(output_stream, [-1.0] * len(floorplan_entries))
                print("Done file appeared before raw trace existed; wrote termination slot.", flush=True)
                return 0
            time.sleep(args.poll)

        with raw_path.open("r", encoding="utf-8") as raw_stream:
            while True:
                offset = raw_stream.tell()
                line = raw_stream.readline()

                if not line:
                    raw_stream.seek(offset)

                    if done_path.exists():
                        write_slot(output_stream, [-1.0] * len(floorplan_entries))
                        terminated = True
                        print(
                            f"Wrote termination slot after {slots_written} adapted slot(s).",
                            flush=True,
                        )
                        break

                    time.sleep(args.poll)
                    continue

                if not line.endswith("\n"):
                    raw_stream.seek(offset)
                    time.sleep(args.poll)
                    continue

                line_number += 1
                values = parse_power_values(line, raw_path, line_number)
                if not values:
                    continue

                adapted_values = adapt_slot(
                    values,
                    floorplan_entries,
                    cluster_count,
                    args.others_power,
                    raw_path,
                    line_number,
                )
                write_slot(output_stream, adapted_values)
                slots_written += 1

    return 0 if terminated else 1


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
