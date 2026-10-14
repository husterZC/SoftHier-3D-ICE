"""Validated activity recordings, leakage tables, and energy-conserving replay."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Interface_scripts"))
from system_contract import load_contract as load_system_contract, thermal_components  # noqa: E402

TEMPERATURE_GRID_C = np.array([25., 40., 55., 70., 85., 100., 115., 125.])


def load_contract(path):
    document = load_system_contract(path)
    if document["contract"]["version"] != 1:
        raise ValueError("thermal replay currently supports single-die version 1 recordings only; memory-on-logic requires live co-simulation")
    return document


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def read_trace(path, paths):
    """Read sequentially, reject gaps, duplicates and truncated captures."""
    ends, dynamic, leakage, temperatures = [], [], [], []
    initialized = finalized = False
    previous = 0
    with Path(path).open() as stream:
        for number, line in enumerate(stream, 1):
            entry = json.loads(line)
            if entry.get("protocol") != {"name": "gvsoc-power-hook", "version": 1}:
                raise ValueError(f"{path}:{number}: unsupported protocol")
            if entry.get("temperature_unit") != "celsius" or finalized:
                raise ValueError(f"{path}:{number}: invalid trace lifecycle")
            phase = entry["phase"]
            start, end = entry["window"]["start_ps"], entry["window"]["end_ps"]
            if any(type(t) is not int for t in (start, end)):
                raise ValueError("trace timestamps must be integer picoseconds")
            if phase == "init":
                if initialized or ends or start != 0 or end != 0 or entry["components"]:
                    raise ValueError("invalid initialization record")
                initialized = True
                continue
            if not initialized or phase not in ("update", "final") or start != previous or end < start:
                raise ValueError(f"{path}:{number}: non-contiguous activity windows")
            finalized = phase == "final"
            if end == start:
                if not finalized or entry["components"]:
                    raise ValueError("unexpected empty activity window")
                continue
            components = entry["components"]
            samples = {c["path"]: c for c in components}
            if len(samples) != len(components) or set(samples) != set(paths):
                raise ValueError(f"{path}:{number}: component mapping differs from contract")
            rows = [samples[p] for p in paths]
            if any(c.get("scope") != "subtree" for c in rows):
                raise ValueError("only exact subtree samples are supported")
            values = np.array([[c[k] for c in rows] for k in
                               ("dynamic_w", "leakage_w", "total_w", "temperature_c")])
            if (not np.isfinite(values).all() or (values[:3] < 0).any()
                    or (values[3] <= -273.15).any()
                    or not np.allclose(values[0] + values[1], values[2], rtol=1e-9, atol=1e-12)):
                raise ValueError(f"{path}:{number}: invalid component power/temperature")
            ends.append(end)
            dynamic.append(values[0])
            leakage.append(values[1])
            temperatures.append(values[3])
            previous = end
    if not initialized or not finalized or not ends:
        raise ValueError("capture is incomplete (requires init, activity and final)")
    return (np.array([0, *ends], dtype=np.int64), np.array(dynamic),
            np.array(leakage), np.array(temperatures))


def roi_from_log(path):
    text = Path(path).read_text(errors="replace")
    starts = re.findall(r"THERMAL_REPLAY_ROI_START_PS=(\d+)", text)
    ends = re.findall(r"THERMAL_REPLAY_ROI_END_PS=(\d+)", text)
    if len(starts) != 1 or len(ends) != 1:
        raise ValueError("requires exactly one timer start/end pair; rebuild GVSoC and capture "
                         "a repeat=1 workload, or supply explicit --layer-start-ps/--layer-end-ps")
    return int(starts[0]), int(ends[0])


class LeakageModel:
    def __init__(self, document, paths, grid=TEMPERATURE_GRID_C):
        metadata = document["metadata"]
        if metadata.get("component_power_model_version") != 2:
            raise ValueError("replay supports component power model version 2")
        specs = metadata["power_model_specs"]
        node = metadata["technology_node"]
        kinds = {c["simulator_path"]: c["kind"] for c in metadata["components"]}
        if set(kinds) != set(paths):
            raise ValueError("component model inventory differs from thermal mapping")
        supported = {"memory", "light_redmule", "core", "spatz", "idma", "floonoc", "transpose"}
        if set(kinds.values()) - supported:
            raise ValueError("unknown component leakage model")
        self.grid = np.asarray(grid, dtype=float)
        if not np.array_equal(self.grid, TEMPERATURE_GRID_C):
            raise ValueError("unsupported archived temperature grid")
        doubles = np.array([float(specs["memory" if kinds[p] == "memory" else "light_redmule"]
                                 ["technology_nodes"][node]["leakage_doubling_temperature_c"])
                            for p in paths])
        if not np.isfinite(doubles).all() or (doubles <= 0).any():
            raise ValueError("invalid leakage doubling temperatures")
        self.table = 2 ** ((self.grid[:, None] - 25.) / doubles)

    def factors(self, temperatures):
        # Match GVSoC's sampled table and linear interpolation, including its
        # clamp. The runner separately marks temperatures above 125 C invalid.
        temperatures = np.clip(temperatures, self.grid[0], self.grid[-1])
        lower = np.minimum(np.searchsorted(self.grid, temperatures, side="right") - 1,
                           len(self.grid) - 2)
        fraction = (temperatures - self.grid[lower]) / (self.grid[lower + 1] - self.grid[lower])
        columns = np.arange(len(temperatures))
        return self.table[lower, columns] * (1 - fraction) + self.table[lower + 1, columns] * fraction


@dataclass
class Activity:
    edges: np.ndarray
    dynamic: np.ndarray
    leakage: np.ndarray
    start: int
    end: int

    def __post_init__(self):
        if (self.edges.ndim != 1 or self.edges[0] != 0 or np.any(np.diff(self.edges) <= 0)
                or not 0 <= self.start < self.end <= self.edges[-1]
                or self.dynamic.ndim != 2 or self.dynamic.shape != self.leakage.shape
                or len(self.dynamic) != len(self.edges) - 1):
            raise ValueError("invalid activity shape, bounds or timestamps")
        if any(not np.isfinite(a).all() or (a < 0).any() for a in (self.dynamic, self.leakage)):
            raise ValueError("activity powers must be finite and nonnegative")
        self.period = self.end - self.start
        self.values = np.concatenate((self.dynamic, self.leakage), axis=1)
        # Picoseconds stay integers when wrapping the period. Convert to seconds
        # only during integration, avoiding drift over thousands of cycles.
        self.integrals = np.vstack((np.zeros(self.values.shape[1]),
                                    np.cumsum(self.values * np.diff(self.edges)[:, None] * 1e-12, axis=0)))
        self.prefix = self.primitive(self.start)
        self.cycle_energy = self.primitive(self.end) - self.prefix

    def primitive(self, time_ps):
        if not 0 <= time_ps <= self.edges[-1]:
            raise ValueError("activity lookup outside recording")
        index = min(int(np.searchsorted(self.edges, time_ps, side="right")) - 1, len(self.values) - 1)
        return self.integrals[index] + self.values[index] * (time_ps - int(self.edges[index])) * 1e-12

    def repeated_integral(self, time_ps):
        if time_ps <= self.start:
            return self.primitive(time_ps)
        cycles, phase = divmod(int(time_ps) - self.start, self.period)
        return self.prefix + cycles * self.cycle_energy + self.primitive(self.start + phase) - self.prefix

    def average(self, start, end):
        if end <= start or start < 0:
            raise ValueError("replay interval must have positive duration")
        # Startup occurs once; only [start, end) of the measured layer repeats.
        power = (self.repeated_integral(end) - self.repeated_integral(start)) / ((end - start) * 1e-12)
        return np.split(np.maximum(power, 0.), 2)


def load_recording(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "recording.json").read_text())
    if manifest.get("format") != "softhier-thermal-replay-v1":
        raise ValueError("unsupported recording format")
    if type(manifest.get("layers_per_cycle", 1)) is not int or manifest.get("layers_per_cycle", 1) < 1:
        raise ValueError("invalid recording layer count")
    if set(manifest["sha256"]) != {"activity.npz", "system_config.json", "ice.stk", "floorplan.flp"}:
        raise ValueError("recording is missing required checksums")
    for name, checksum in manifest["sha256"].items():
        if Path(name).name != name or digest(directory / name) != checksum:
            raise ValueError(f"recording checksum mismatch: {name}")
    document = load_contract(directory / "system_config.json")
    paths = [c["path"] for c in thermal_components(document)]
    if manifest["component_paths"] != paths:
        raise ValueError("recording component order differs from contract")
    with np.load(directory / "activity.npz", allow_pickle=False) as arrays:
        activity = Activity(arrays["edges_ps"], arrays["dynamic_w"], arrays["leakage_w"],
                            manifest["layer_start_ps"], manifest["layer_end_ps"])
    if activity.dynamic.shape[1] != len(paths) or manifest["period_ps"] != activity.period:
        raise ValueError("recording dimensions or period differ from manifest")
    return manifest, document, activity
