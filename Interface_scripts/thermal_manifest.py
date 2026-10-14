#!/usr/bin/env python3
"""Read and manage generated per-die 3D-ICE artifacts."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


OUTPUT_KEYS = ("temperature_output", "map_output", "coordinates_output")


def load_manifest(path):
    path = Path(path).resolve()
    document = json.loads(path.read_text())
    if type(document.get("version")) is not int or document["version"] != 1:
        raise ValueError("unsupported thermal manifest version")
    dies = document.get("dies")
    if not isinstance(dies, list) or not dies:
        raise ValueError("thermal manifest must declare at least one die")
    identifiers, entries, outputs = set(), [], set()
    for die in dies:
        identifier = die.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", identifier):
            raise ValueError("invalid thermal die identifier")
        if identifier in identifiers:
            raise ValueError("duplicate thermal die identifier")
        identifiers.add(identifier)
        for key in ("floorplan", *OUTPUT_KEYS):
            value = die.get(key)
            if not isinstance(value, str) or not value:
                raise ValueError(f"missing {key} for die {identifier}")
            candidate = Path(value)
            if key in OUTPUT_KEYS:
                if candidate.is_absolute() or ".." in candidate.parts:
                    raise ValueError("thermal output paths must stay inside the run directory")
                if value in outputs:
                    raise ValueError("thermal output paths must be unique")
                outputs.add(value)
        names = die.get("elements")
        if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n for n in names):
            raise ValueError(f"invalid floorplan element list for {identifier}")
        entries.extend(names)
    if len(entries) != len(set(entries)):
        raise ValueError("thermal manifest floorplan elements must be globally unique")
    order = document.get("power_order")
    if not isinstance(order, list) or len(order) != len(entries) or set(order) != set(entries):
        raise ValueError("thermal manifest power order must cover every floorplan element once")
    # dies are emitted in native source-layer order by the generator.
    if order != entries:
        raise ValueError("thermal manifest die order disagrees with native power order")
    document["_path"] = str(path)
    return document


def artifact_path(manifest, die, key):
    return (Path(manifest["_path"]).parent / die[key]).resolve()


def copy_inputs(source, destination, source_stack=None, destination_stack=None,
                single_die_floorplan=None):
    manifest = load_manifest(source)
    destination = Path(destination).resolve()
    if (source_stack is None) != (destination_stack is None):
        raise ValueError("source-stack and destination-stack must be supplied together")
    planned, targets = [], set()
    for die in manifest["dies"]:
        original = artifact_path(manifest, die, "floorplan")
        target = (Path(single_die_floorplan).resolve() if single_die_floorplan is not None and len(manifest["dies"]) == 1
                  else destination.parent / original.name)
        if target in targets:
            raise ValueError("floorplan filenames collide when staging runtime inputs")
        targets.add(target)
        planned.append((die, original, target))
    stack_contents = None
    if source_stack is not None:
        source_stack, destination_stack = Path(source_stack).resolve(), Path(destination_stack).resolve()
        by_die = {die["id"]: (original, target) for die, original, target in planned}
        seen = set()

        def replace_floorplan(match):
            identifier = match.group("die")
            if identifier not in by_die or identifier in seen:
                raise ValueError("stack powered dies differ from the thermal manifest")
            seen.add(identifier)
            original, target = by_die[identifier]
            if (source_stack.parent / match.group("path")).resolve() != original:
                raise ValueError(f"stack floorplan differs from manifest for {identifier}")
            relative = os.path.relpath(target, destination_stack.parent)
            if any(character in relative for character in ('"', '\n', '\r')):
                raise ValueError("runtime floorplan path cannot contain quotes or newlines")
            return match.group("prefix") + relative + '"'

        pattern = r'(?P<prefix>^\s*die\s+(?P<die>[A-Za-z0-9_.-]+)\s+\S+\s+floorplan\s+")(?P<path>[^"]+)"'
        stack_contents = re.sub(pattern, replace_floorplan, source_stack.read_text(), flags=re.MULTILINE)
        if seen != set(by_die):
            raise ValueError("stack does not reference every manifest die")
    destination.parent.mkdir(parents=True, exist_ok=True)
    for die, original, target in planned:
        target.parent.mkdir(parents=True, exist_ok=True)
        if original != target:
            shutil.copy2(original, target)
        die["floorplan"] = os.path.relpath(target, destination.parent)
    if stack_contents is not None:
        destination_stack.parent.mkdir(parents=True, exist_ok=True)
        destination_stack.write_text(stack_contents)
    manifest.pop("_path")
    destination.write_text(json.dumps(manifest, indent=2) + "\n")


def temperature_stats(path):
    rows, maximum = 0, None
    if path.is_file():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("%"):
                values = list(map(float, line.split()))
                rows += 1
                candidate = max(values[1:])
                maximum = candidate if maximum is None else max(maximum, candidate)
    return rows, maximum


def gif_paths(manifest, path):
    path = Path(path)
    return [(die["id"], path if len(manifest["dies"]) == 1 else path.with_name(
             f"{path.stem}_{die['id'].lower()}{path.suffix}")) for die in manifest["dies"]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("copy", "width", "primary", "summary", "clean-outputs", "render", "gif-paths"))
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--source-stack", type=Path)
    parser.add_argument("--destination-stack", type=Path)
    parser.add_argument("--single-die-floorplan", type=Path)
    parser.add_argument("--gif", type=Path)
    parser.add_argument("--stride", type=int, default=1)
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--fps", type=int, default=8)
    parser.add_argument("--writer", default="auto")
    parser.add_argument("--slot-seconds", type=float)
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    if args.action == "copy":
        if args.destination is None:
            parser.error("copy requires --destination")
        copy_inputs(args.manifest, args.destination, args.source_stack, args.destination_stack,
                    args.single_die_floorplan)
    elif args.action == "width":
        print(len(manifest["power_order"]))
    elif args.action == "primary":
        print(artifact_path(manifest, manifest["dies"][0], "temperature_output"))
    elif args.action == "summary":
        maxima = []
        for die in manifest["dies"]:
            path = artifact_path(manifest, die, "temperature_output")
            rows, maximum = temperature_stats(path)
            print(f"3dice_{die['id']}_tflp_rows: {rows}")
            print(f"{die['id']}_max_temperature_k: {'n/a' if maximum is None else f'{maximum:.3f}'}")
            print(f"{die['id']}_temperatures: {path}")
            if maximum is not None:
                maxima.append(maximum)
        print(f"max_temperature_k: {'n/a' if not maxima else f'{max(maxima):.3f}'}")
    elif args.action == "gif-paths":
        if args.gif is None:
            parser.error("gif-paths requires --gif")
        for identifier, path in gif_paths(manifest, args.gif):
            print(f"temperature GIF ({identifier}): {path}")
    elif args.action == "clean-outputs":
        for die in manifest["dies"]:
            for key in OUTPUT_KEYS:
                path = artifact_path(manifest, die, key)
                if path.exists():
                    path.unlink()
    else:
        if args.gif is None:
            parser.error("render requires --gif")
        renderer = Path(__file__).parent / "plot_runtime_temperature_map/plot_runtime_tmap.py"
        for die, (_, output) in zip(manifest["dies"], gif_paths(manifest, args.gif)):
            output.parent.mkdir(parents=True, exist_ok=True)
            command = [sys.executable, str(renderer),
                "--coords", str(artifact_path(manifest, die, "coordinates_output")),
                "--map", str(artifact_path(manifest, die, "map_output")),
                "--gif", str(output), "--once", "--gif-stride", str(args.stride),
                "--gif-width", str(args.width), "--gif-fps", str(args.fps), "--gif-writer", args.writer]
            if args.slot_seconds is not None:
                command += ["--slot-seconds", str(args.slot_seconds)]
            subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
