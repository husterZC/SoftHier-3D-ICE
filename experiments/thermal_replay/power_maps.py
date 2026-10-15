"""Export or plot power in the saved replay interval ending at an exact time."""

import argparse
import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import gzip
import json
import math
import os
from pathlib import Path
import re

import numpy as np

from .activity import digest, write_json
from .plot_stack import NUMBER, output_directory, read_stack, uncomment
from .replay import Mapping
from system_contract import load_contract, thermal_components


GEOMETRY = ("name", "x_um", "y_um", "width_um", "height_um")
POWERS = ("dynamic_w", "leakage_w", "constant_w", "total_w")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def time_ps(seconds):
    try:
        value = Decimal(str(seconds))
        require(value.is_finite() and value > 0,
                "time must be positive and exactly representable in integer picoseconds")
        numerator, denominator = value.as_integer_ratio()
        whole, remainder = divmod(numerator * 10**12, denominator)
        require(remainder == 0, "time must be exactly representable in integer picoseconds")
        return whole
    except InvalidOperation as error:
        raise ValueError("invalid time in seconds") from error


def read_sample(path, target, slot, columns):
    """Stream to one exact slot end; never load the component history into RAM."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", newline="") as stream:
        header = stream.readline()
        require(header.endswith("\n"), f"{path}: missing or truncated header")
        require(next(csv.reader([header])) == ["time_ps", *columns], f"{path}: wrong columns")
        for index, line in enumerate(stream, 1):
            require(line.endswith("\n") and line.count(",") == len(columns), f"{path}: truncated or malformed row")
            stamp = int(line.partition(",")[0])
            require(stamp == index * slot, f"{path}: non-contiguous power timestamps")
            if stamp == target:
                values = np.array(next(csv.reader([line]))[1:], dtype=float)
                require(np.isfinite(values).all(), f"{path}: nonfinite sample")
                return values
            if stamp > target:
                break
    raise ValueError(f"{path}: no saved interval ending at {target} ps")


def floorplan_blocks(text):
    """Keep the original geometry/discretization text when writing FLP powers."""
    text = uncomment(text)
    matches = list(re.finditer(r"(?m)^\s*([A-Za-z0-9_.-]+)\s*:", text))
    require(matches and not text[:matches[0].start()].strip(), "unsupported or empty floorplan")
    blocks = []
    for i, match in enumerate(matches):
        body = text[match.end():matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        body = re.sub(r"\bpower\s+values\b[^;]*;", "", body)
        geometry = re.fullmatch(rf"\s*position\s+({NUMBER})\s*,\s*({NUMBER})\s*;\s*"
                                rf"dimension\s+({NUMBER})\s*,\s*({NUMBER})\s*;\s*"
                                r"(?:discretization\s+([1-9]\d*)\s*,\s*([1-9]\d*)\s*;\s*)?", body)
        require(geometry is not None, f"{match[1]}: expected one rectangular floorplan region")
        values = [float(v) for v in geometry.groups()[:4]]
        require(np.isfinite(values).all() and min(values[:2]) >= 0 and min(values[2:]) > 0,
                f"{match[1]}: invalid floorplan geometry")
        blocks.append(({"name": match[1], **dict(zip(GEOMETRY[1:], values))}, body.rstrip()))
    require(len({region["name"] for region, _ in blocks}) == len(blocks), "duplicate floorplan region")
    return blocks


@dataclass
class Snapshot:
    run: Path
    recording: Path
    blocks: list
    regions: list
    dynamic: np.ndarray
    stack: dict
    metadata: dict


def read_snapshot(run, seconds="1"):
    run = Path(run).resolve()
    summary = json.loads((run / "summary.json").read_text())
    require(summary["status"] in {"complete", "time_limit", "model_limit", "converged"},
            "requires a completed replay (running, failed, and interrupted runs are unsupported)")
    require(not summary["single_pass"], "single-pass traces can have partial power windows; use a repeated replay")
    target, slot = time_ps(seconds), summary["slot_ps"]
    require(type(slot) is int and slot > 0 and target % slot == 0,
            "time must be an exact saved power-slot endpoint; no interpolation is performed")
    require(0 < target <= summary["slots"] * slot, "requested time is outside the saved replay")
    recording = Path(summary["recording"])
    if not recording.is_absolute():
        recording = run / recording
    recording = recording.resolve()
    manifest = json.loads((recording / "recording.json").read_text())
    require(manifest.get("format") == "softhier-thermal-replay-v1", "unsupported recording format")
    checksums = manifest["sha256"]
    require(checksums == summary["recording_sha256"], "recording checksums differ from the replay")
    require(set(checksums) == {"activity.npz", "system_config.json", "ice.stk", "floorplan.flp"},
            "recording is missing required checksums")
    for name, checksum in checksums.items():
        require(digest(recording / name) == checksum, f"recording checksum mismatch: {name}")
    require(digest(run / "floorplan.flp") == checksums["floorplan.flp"], "runtime floorplan differs from recording")
    document = load_contract(recording / "system_config.json")
    components = json.loads((run / "components.json").read_text())
    paths = manifest["component_paths"]
    require(components["paths"] == paths and components["mapping"] == thermal_components(document),
            "replay component mapping differs from recording")
    require(paths == [c["path"] for c in components["mapping"]], "component column order differs from contract")
    mapping = Mapping(document, run / "floorplan.flp")
    blocks = floorplan_blocks((run / "floorplan.flp").read_text())
    require([r["name"] for r, _ in blocks] == mapping.entries, "floorplan region order differs from mapping")
    stack = read_stack(run / "ice.stk")
    require(time_ps(stack["slot_seconds"]) == slot and time_ps(stack["step_seconds"]) == summary["step_ps"],
            "runtime solver settings differ from summary")
    require(manifest["power_interval_ps"] == slot, "recording power interval differs from replay")
    length, width = stack["footprint_um"]
    for region, _ in blocks:
        require(region["x_um"] + region["width_um"] <= length + 1e-6
                and region["y_um"] + region["height_um"] <= width + 1e-6,
                f'{region["name"]}: outside the chip footprint')
    dynamic = read_sample(run / "dynamic_power_w.csv.gz", target, slot, paths)
    leakage = read_sample(run / "leakage_power_w.csv.gz", target, slot, paths)
    require((dynamic >= 0).all() and (leakage >= 0).all(), "component powers must be nonnegative")
    selected = read_sample(run / "timeseries.csv", target, slot,
                           ["dynamic_w", "leakage_w", "constant_w", "peak_component_c"])
    # Mapping.powers includes fixed regions. Split these out so they are counted
    # once, independently of component dynamic and leakage power.
    regional_dynamic = np.where(mapping.power_indices >= 0, dynamic[mapping.power_indices], 0.)
    regional_leakage = np.where(mapping.power_indices >= 0, leakage[mapping.power_indices], 0.)
    regional_total = mapping.powers(dynamic + leakage)
    sums = [regional_dynamic.sum(), regional_leakage.sum(), mapping.constants.sum()]
    require(np.allclose(sums, selected[:3], rtol=1e-9, atol=1e-10),
            "regional dynamic/leakage/fixed totals differ from timeseries")
    require(np.isclose(regional_total.sum(), sum(sums), rtol=1e-9, atol=1e-10), "regional total power mismatch")
    regions = [{**geometry, **dict(zip(POWERS, map(float, values)))} for (geometry, _), values in
               zip(blocks, zip(regional_dynamic, regional_leakage, mapping.constants, regional_total))]
    source_names = ("summary.json", "components.json", "ice.stk", "floorplan.flp", "timeseries.csv",
                    "dynamic_power_w.csv.gz", "leakage_power_w.csv.gz")
    batch = re.search(r"_b(\d+)_(?:constant|temperature_aware)$", Path(manifest.get("source_run", "")).name)
    metadata = {"run": str(run), "recording": str(recording), "profile": summary["profile"],
                "time_ps": target, "time_seconds": target * 1e-12,
                "interval_start_ps": target - slot, "interval_end_ps": target,
                "slot_ps": slot, "step_ps": summary["step_ps"], "region_count": len(regions),
                "batch": int(batch[1]) if batch else None, "footprint_um": [length, width],
                "chip_area_mm2": length * width * 1e-6,
                **dict(zip(POWERS, map(float, [*sums, regional_total.sum()]))),
                "average_power_density_w_per_mm2": float(regional_total.sum()) / (length * width * 1e-6),
                "heat_transfer_coefficient_w_per_m2_k": stack["heat_transfer_coefficient_w_per_m2_k"],
                "recording_sha256": checksums, "source_sha256": {name: digest(run / name) for name in source_names},
                "power_unit": "W", "geometry_unit": "um", "sample_kind": "mean power over the interval ending at time_ps"}
    return Snapshot(run, recording, blocks, regions, dynamic, stack, metadata)


def write_flp(path, snapshot):
    m = snapshot.metadata
    header = ("// Saved interval power; geometry in um, power values in W.\n"
              f'// Interval [{m["interval_start_ps"]}, {m["interval_end_ps"]}] ps; profile {m["profile"]}.\n')
    path.write_text(header + "\n".join(f'{region["name"]} :{body}\n    power values {region["total_w"]:.15g};\n'
                                        for (_, body), region in zip(snapshot.blocks, snapshot.regions)))


def write_csv(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def export(run, output, seconds="1"):
    snapshot = read_snapshot(run, seconds)
    output = output_directory(output, [snapshot.run, snapshot.recording])
    write_flp(output / "power_map.flp", snapshot)
    write_csv(output / "power_map_regions.csv", snapshot.regions)
    write_json(output / "metadata.json", snapshot.metadata)
    return snapshot.metadata


def validate_pair(constant, aware):
    require(constant.metadata["profile"] == "constant" and aware.metadata["profile"] == "temperature_aware",
            "expected constant and temperature_aware profiles, respectively")
    for field in ("time_ps", "slot_ps", "step_ps", "recording_sha256"):
        require(constant.metadata[field] == aware.metadata[field], f"paired {field} differs")
    require(constant.stack == aware.stack, "paired thermal stacks differ")
    require([[r[k] for k in GEOMETRY] for r in constant.regions] ==
            [[r[k] for k in GEOMETRY] for r in aware.regions], "paired floorplan geometry differs")
    require(np.allclose(constant.dynamic, aware.dynamic, rtol=1e-9, atol=1e-12),
            "paired component dynamic activity differs at the selected time")


def plot(constant_run, aware_run, output, seconds="1"):
    constant, aware = (read_snapshot(run, seconds) for run in (constant_run, aware_run))
    validate_pair(constant, aware)
    output = output_directory(output, [constant.run, constant.recording, aware.run, aware.recording])
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/thermal-replay-analysis-mpl")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PatchCollection
    from matplotlib.colors import Normalize
    from matplotlib.patches import Rectangle
    from matplotlib.ticker import MaxNLocator

    vmax = max(10., math.ceil(max(r["total_w"] * 1000 for s in (constant, aware) for r in s.regions) / 10) * 10)
    norm = Normalize(0, vmax)
    m = aware.metadata
    length, width = (v / 1000 for v in m["footprint_um"])
    with plt.rc_context({"font.family": "DejaVu Sans", "pdf.fonttype": 42, "svg.fonttype": "none"}):
        fig = plt.figure(figsize=(13.2, 7.0))
        fig.text(.075, .951, f'Chip power at t = {m["time_seconds"]:g} s', fontsize=21, weight="bold")
        workload = f'B{m["batch"]} replay' if m["batch"] else "Thermal replay"
        fig.text(.075, .905, f'{workload} · h = {m["heat_transfer_coefficient_w_per_m2_k"]:,.0f} W/(m²·K)',
                 fontsize=11.5, color="#536170")
        for x, snapshot, title in ((.075, constant, "Constant leakage"), (.505, aware, "Temperature-aware")):
            axis = fig.add_axes([x, .19, .355, .57])
            patches = [Rectangle((r["x_um"] / 1000, r["y_um"] / 1000),
                                 r["width_um"] / 1000, r["height_um"] / 1000) for r in snapshot.regions]
            collection = PatchCollection(patches, cmap="turbo", norm=norm, edgecolors=(0, 0, 0, .2),
                                         linewidths=.25, antialiaseds=False)
            collection.set_array(np.array([r["total_w"] * 1000 for r in snapshot.regions]))
            axis.add_collection(collection)
            axis.set(xlim=(0, length), ylim=(0, width), xlabel="x (mm)", ylabel="y (mm)", aspect="equal")
            axis.xaxis.set_major_locator(MaxNLocator(nbins=6))
            axis.yaxis.set_major_locator(MaxNLocator(nbins=6))
            s = snapshot.metadata
            fig.text(x + .1775, .862, title, fontsize=13, weight="bold", ha="center")
            fig.text(x + .1775, .826, f'Total power: {s["total_w"]:.3f} W | Avg. density: '
                     f'{s["average_power_density_w_per_mm2"]:.3f} W/mm²', fontsize=11.5, ha="center")
            split = f'Dynamic: {s["dynamic_w"]:.3f} W | Leakage: {s["leakage_w"]:.3f} W'
            if s["constant_w"]:
                split += f' | Fixed: {s["constant_w"]:.3f} W'
            fig.text(x + .1775, .790, split, fontsize=11, ha="center")
        fig.colorbar(collection, cax=fig.add_axes([.891, .203, .017, .544]), label="Region power (mW)")
        fixed = " + fixed" if constant.metadata["constant_w"] or m["constant_w"] else ""
        fig.text(.075, .086, f"Dynamic + leakage{fixed} power in each floorplan region · shared linear turbo scale",
                 fontsize=11, color="#536170")
        fig.text(.075, .049, f'Interval: {m["interval_start_ps"] * 1e-12:g}–{m["time_seconds"]:g} s'
                 f' · Footprint: {length:.3f} × {width:.3f} mm²', fontsize=10.5, color="#536170")
        for extension in ("png", "pdf", "svg"):
            fig.savefig(output / f"power_maps.{extension}", dpi=320, facecolor="white")
        plt.close(fig)
    write_flp(output / "constant_power_map.flp", constant)
    write_flp(output / "temperature_aware_power_map.flp", aware)
    rows = [{**{k: c[k] for k in GEOMETRY}, **{f"{profile}_{k}": r[k] for profile, r in
             (("constant", c), ("temperature_aware", a)) for k in POWERS}} for c, a in zip(constant.regions, aware.regions)]
    write_csv(output / "power_map_regions.csv", rows)
    metadata = {"constant": constant.metadata, "temperature_aware": aware.metadata,
                "colormap": "turbo", "scale": "linear", "color_unit": "mW per floorplan region",
                "color_limits_mw": [0, vmax], "density_definition": "total chip power / full chip footprint area"}
    write_json(output / "plot_metadata.json", metadata)
    return metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    single = commands.add_parser("export", help="Write a total-power FLP, regional CSV and provenance JSON")
    single.add_argument("--run", type=Path, required=True)
    paired = commands.add_parser("plot", help="Plot a constant/temperature-aware pair with a shared turbo scale")
    paired.add_argument("--constant-run", type=Path, required=True)
    paired.add_argument("--aware-run", type=Path, required=True)
    for command in (single, paired):
        command.add_argument("--time-seconds", default="1", help="Exact saved interval end in seconds (default: 1)")
        command.add_argument("--output", type=Path, required=True, help="Output directory outside source runs/recordings")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            export(args.run, args.output, args.time_seconds)
        else:
            plot(args.constant_run, args.aware_run, args.output, args.time_seconds)
    except (ValueError, OSError, KeyError, EOFError) as error:
        parser.exit(1, f"Power map failed: {error}\n")
    print(f"Saved {args.command} outputs in {args.output}")


if __name__ == "__main__":
    main()
