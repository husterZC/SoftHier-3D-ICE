"""One validated view of single- and multi-die thermal artifacts."""

from pathlib import Path
import re
import sys

from .activity import ROOT

sys.path.insert(0, str(ROOT / "Interface_scripts/co-simulation"))
from ice_trace_adapter import parse_floorplan_entries  # noqa: E402
from system_contract import flattened_floorplan_name, thermal_die_ids  # noqa: E402
from thermal_manifest import artifact_path, load_manifest  # noqa: E402


class Layout:
    def __init__(self, directory, document, floorplan=None):
        self.directory = Path(directory).resolve()
        self.multidie = document["contract"]["version"] == 2
        if self.multidie:
            manifest = load_manifest(self.directory / "thermal_manifest.json")
            self.dies = [dict(die, floorplan=artifact_path(manifest, die, "floorplan"))
                         for die in manifest["dies"]]
            if [d["id"] for d in self.dies] != thermal_die_ids(document):
                raise ValueError("thermal manifest die order differs from native source order")
        else:
            # Generated source runs may contain both a powered FLP and the
            # geometry-only input. Record the latter, as the V1 workflow did.
            path = Path(floorplan) if floorplan else self.directory / "floorplan_nopower.flp"
            if floorplan is None and not path.is_file():
                path = self.directory / "floorplan.flp"
            self.dies = [{"id": "TOP_DIE", "floorplan": path.resolve(),
                          "elements": parse_floorplan_entries(path),
                          "temperature_output": "floorplan_temperatures.txt",
                          "map_output": "temperature_map.txt"}]
        for die in self.dies:
            names = parse_floorplan_entries(die["floorplan"])
            expected = {flattened_floorplan_name(e["name"]) for e in document["floorplan"]["elements"]
                        if e.get("die", "TOP_DIE") == die["id"]}
            if names != die["elements"] or set(names) != expected or len(set(names)) != len(names):
                raise ValueError(f"floorplan order or die assignment differs for {die['id']}")
        self.entries = [name for die in self.dies for name in die["elements"]]
        if len(set(self.entries)) != len(self.entries):
            raise ValueError("duplicate floorplan regions across dies")
        if self.multidie:
            if manifest["power_order"] != self.entries:
                raise ValueError("thermal manifest power order differs from floorplans")
            stack = (self.directory / "ice.stk").read_text()
            declarations = re.findall(r'^\s*die\s+(\w+)\s+\w+\s+floorplan\s+"([^"]+)"', stack, re.M)
            if [name for name, _ in reversed(declarations)] != [d["id"] for d in self.dies]:
                raise ValueError("stack source order differs from thermal manifest")
            by_die = dict(declarations)
            for die in self.dies:
                if (self.directory / by_die[die["id"]]).resolve() != die["floorplan"]:
                    raise ValueError(f"stack floorplan differs for {die['id']}")

    @property
    def inputs(self):
        files = [d["floorplan"] for d in self.dies]
        if self.multidie:
            files.append(self.directory / "thermal_manifest.json")
        return files

    @property
    def outputs(self):
        # Preserve the legacy client/mocks' list-of-names interface.
        return self.dies if self.multidie else self.entries

    def select(self, die=None):
        selected = [d for d in self.dies if die is None or d["id"] == die]
        if not selected:
            raise ValueError(f"unknown die {die!r}; choose from {', '.join(d['id'] for d in self.dies)}")
        return selected


def recording_files(directory, document):
    directory = Path(directory).resolve()
    layout = Layout(directory, document)
    names = {"activity.npz", "system_config.json", "ice.stk"}
    for path in layout.inputs:
        if path.parent != directory:
            raise ValueError("recording thermal inputs must be local files in the archive")
        names.add(path.name)
    return names
