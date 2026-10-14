#!/usr/bin/env python3
"""Enable HBM2 core power in a private, content-addressed configuration copy."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from export_system_config import import_architecture

REPO = Path(__file__).resolve().parents[3]


def inside(path, root=REPO):
    path = Path(path).resolve()
    path.relative_to(root)
    return path


def prepare(arch, source, workdir):
    source, workdir = inside(source), inside(workdir)
    architecture = import_architecture(inside(arch))
    if not getattr(architecture, "dram3d_enable", 0):
        return source
    if getattr(architecture, "dram3d_type", "hbm2-example.json") != "hbm2-example.json":
        raise ValueError("stacked DRAM power currently requires hbm2-example.json")
    configs = inside(source / "dramsys_configs")
    simulation = json.loads((configs / "hbm2-example.json").read_text())
    settings = inside(configs / simulation["simulation"]["simconfig"], configs)
    document = json.loads(settings.read_text())
    if document["simconfig"].get("PowerAnalysis") is True:
        return source
    digest = hashlib.sha256(Path(__file__).read_bytes())
    for path in sorted(configs.rglob("*")):
        if path.is_file():
            inside(path, configs)
            digest.update(str(path.relative_to(configs)).encode())
            digest.update(path.read_bytes())
    destination = workdir / "dram3d_configs" / digest.hexdigest()
    marker = destination / "ready.json"
    if not marker.exists():
        if destination.exists():
            raise ValueError("incomplete provider configuration stage: {}".format(destination))
        shutil.copytree(str(configs), str(destination / "dramsys_configs"))
        document["simconfig"].update(PowerAnalysis=True, EnableWindowing=False)
        # Other memories can share the original settings; change only HBM2.
        relative = "simconfig/softhier_dram3d_power.json"
        (destination / "dramsys_configs" / relative).write_text(json.dumps(document, indent=2) + "\n")
        simulation["simulation"]["simconfig"] = relative
        (destination / "dramsys_configs/hbm2-example.json").write_text(json.dumps(simulation, indent=2) + "\n")
        marker.write_text(json.dumps({"source": str(source), "input_sha256": digest.hexdigest()}, indent=2) + "\n")
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.arch, args.source, args.workdir))


if __name__ == "__main__":
    main()
