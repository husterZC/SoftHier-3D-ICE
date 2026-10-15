"""Plot a saved constant/temperature-aware replay pair; never run a simulator."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import html
from itertools import islice
import json
import math
import os
from pathlib import Path
import re
import runpy

os.environ.setdefault("MPLCONFIGDIR", "/tmp/thermal-replay-analysis-mpl")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation, PillowWriter
from matplotlib.collections import PatchCollection
from matplotlib.patches import Rectangle
import numpy as np

from .activity import ROOT, LeakageModel, digest, load_recording, write_json
from .replay import Mapping

KINDS = ["core", "spatz", "light_redmule", "memory", "idma", "floonoc", "transpose"]
LABELS = dict(zip(KINDS, ["Scalar cores", "Spatz", "RedMulE", "SRAM", "iDMA", "NoC + NI", "Transpose"]))
COLORS = dict(zip(KINDS, ["#0072B2", "#009E73", "#D55E00", "#56B4E9", "#CC79A7", "#E69F00", "#777777"]))
KINDS.append("dram3d_channel")
LABELS["dram3d_channel"] = "DRAM channels"
COLORS["dram3d_channel"] = "#332288"
PROFILE_LABELS = {"constant": "Constant leakage", "temperature_aware": "Temperature-aware"}
BLUE, ORANGE = "#0072B2", "#D55E00"
VIEW = runpy.run_path(str(ROOT / "Interface_scripts/plot_runtime_temperature_map/plot_runtime_tmap.py"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def close(actual, expected, message, atol=1e-8, rtol=1e-9):
    require(np.allclose(actual, expected, atol=atol, rtol=rtol), message)
    return float(np.max(np.abs(np.asarray(actual) - np.asarray(expected))))


def wide_chunks(path, paths, usecols=None):
    """Bound working memory while retaining exact saved row/column ordering."""
    with gzip.open(path, "rt") as stream:
        require(next(csv.reader([stream.readline()])) == ["time_ps", *paths], f"{path}: wrong columns")
        offset = 0
        while lines := list(islice(stream, 2048)):
            data = np.loadtxt(lines, delimiter=",", ndmin=2, usecols=usecols)
            width = len(paths) + 1 if usecols is None else len(usecols)
            require(data.shape == (len(lines), width) and np.isfinite(data).all(), f"{path}: invalid data")
            yield offset, data
            offset += len(data)


def bins(times, values, size):
    starts = np.arange(0, len(times), size)
    counts = np.minimum(size, len(times) - starts)
    return (np.add.reduceat(times, starts) / counts,
            np.add.reduceat(values, starts) / counts,
            np.minimum.reduceat(values, starts), np.maximum.reduceat(values, starts))


def envelope(axis, run, values, color, label=None, scale=1):
    t, mean, low, high = bins(run.time_s, values * scale, run.bin_size)
    axis.fill_between(t, low, high, color=color, alpha=.14, linewidth=0)
    axis.plot(t, mean, color=color, lw=1.1, label=label)


def csv_file(path, rows, fieldnames=None):
    if rows or fieldnames:
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames or list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def read_cycles(path, summary):
    """Read block boundaries, ignoring judgment fields from older runs."""
    rows = []
    with path.open() as stream:
        reader = csv.DictReader(stream)
        require({"cycle", "time_ps"} <= set(reader.fieldnames or []), "Missing cycle boundary columns")
        for i, row in enumerate(reader, 1):
            cycle, time_ps = int(row["cycle"]), int(row["time_ps"])
            layers = int(row.get("layers_completed", cycle * summary["layers_per_cycle"]))
            require(cycle == i and time_ps == summary["layer_start_ps"] + i * summary["period_ps"],
                    "Cycle boundary mismatch")
            require(layers == cycle * summary["layers_per_cycle"], "Layer count mismatch")
            rows.append({"cycle": cycle, "time_ps": time_ps, "layers_completed": layers})
    require(len(rows) == summary["cycles_completed"], "Cycle count mismatch")
    require(summary["layers_completed"] == len(rows) * summary["layers_per_cycle"], "Layer count mismatch")
    return rows


class ReplayRun:
    def __init__(self, path, profile):
        self.path = path.resolve()
        print(f"Reading {self.path.name}", flush=True)
        self.summary = s = json.loads((path / "summary.json").read_text())
        require(s["profile"] == profile, f"{path}: expected {profile} profile")
        require(s["status"] in {"time_limit", "complete", "converged", "model_limit"},
                f"{path}: requires a completed run, found {s['status']}")
        require(not s["single_pass"], "This analyzer requires repeated replays, not single-pass traces")
        self.profile = profile
        self.manifest, self.document, _ = load_recording(s["recording"])
        require(self.manifest["sha256"] == s["recording_sha256"], f"{path}: recording checksums differ")
        components = json.loads((path / "components.json").read_text())
        self.paths, self.mapping = components["paths"], components["mapping"]
        require(self.paths == self.manifest["component_paths"], "Recording/component order mismatch")
        require([m["path"] for m in self.mapping] == self.paths, "Floorplan/component order mismatch")
        native = Mapping(self.document, self.path)
        self.layout, self.die_groups = native.layout, native.component_groups
        from .activity import thermal_components
        require(self.mapping == thermal_components(self.document), "Archived component mapping differs")
        for source in self.layout.inputs:
            require(digest(source) == self.manifest["sha256"][source.name], f"Runtime input differs: {source.name}")
        self.inventory = {c["simulator_path"]: c for c in self.document["metadata"]["components"]}
        present = {self.inventory[p]["kind"] for p in self.paths}
        self.kinds = [kind for kind in KINDS if kind in present]
        self.groups = [np.array([i for i, p in enumerate(self.paths) if self.inventory[p]["kind"] == kind])
                       for kind in self.kinds]
        require(sum(map(len, self.groups)) == len(self.paths), "Unsupported component kind")
        self.logic = np.array([self.inventory[p]["kind"] != "dram3d_channel" for p in self.paths])
        self.die_power = {die: {} for die in self.die_groups}
        self.die_peak_c = {}
        self.series = np.atleast_1d(np.genfromtxt(path / "timeseries.csv", delimiter=",", names=True))
        self.n, self.slot_ps = len(self.series), s["slot_ps"]
        self.slot_s, self.duration = self.slot_ps * 1e-12, s["simulated_seconds"]
        self.time_ps = np.arange(1, self.n + 1, dtype=np.int64) * self.slot_ps
        self.time_s = self.time_ps * 1e-12
        require(self.n == s["slots"] and self.n > 0, "Slot count mismatch")
        require(np.array_equal(self.series["time_ps"], self.time_ps), "Power time grid mismatch")
        require(all(np.isfinite(self.series[name]).all() for name in self.series.dtype.names), "Nonfinite timeseries")
        close(self.time_s[-1], self.duration, "Duration mismatch", atol=1e-12)
        self.bin_size = max(1, round(.001 / self.slot_s))
        stride = max(1, round(.01 / self.slot_s))
        self.frame_indices = np.unique(np.r_[0, np.arange(stride - 1, self.n, stride), self.n - 1]).astype(int)
        self.validation = []
        with np.load(path / "final_state.npz") as final:
            self.final_c = final["component_temperatures_c"].copy()
            self.final_energy = {k: final[k + "_energy_j"].copy() for k in ("dynamic", "leakage")}
        self.read_temperatures()
        self.energy, self.kind_power = {}, {}
        for field in ("dynamic", "leakage"):
            self.read_power(field)
        self.total_w = sum(self.series[k] for k in ("dynamic_w", "leakage_w", "constant_w"))
        self.check("total_energy_j", self.total_w.sum() * self.slot_s, s["total_energy_j"])
        self.check("floorplan_constant_energy_j", self.series["constant_w"].sum() * self.slot_s,
                   s["floorplan_constant_energy_j"])
        self.die_metrics = {}
        for die, group in self.die_groups.items():
            fixed = float(native.constants[native.region_groups[die]].sum())
            self.die_power[die]["constant"] = np.full(self.n, fixed)
            dynamic, leakage = (float(self.energy[k][group].sum()) for k in ("dynamic", "leakage"))
            metrics = {"dynamic_energy_j": dynamic, "leakage_energy_j": leakage,
                       "floorplan_constant_energy_j": fixed * self.duration,
                       "total_energy_j": dynamic + leakage + fixed * self.duration,
                       "peak_component_c": float(self.die_peak_c[die].max()),
                       "final_peak_component_c": float(self.die_peak_c[die][-1])}
            if self.layout.multidie or "per_die" in s:
                for key, value in metrics.items():
                    self.check(f"{die}_{key}", value, s["per_die"][die][key])
            self.die_metrics[die] = metrics
        if self.layout.multidie:
            self.read_die_series()
        self.cycles = read_cycles(path / "cycles.csv", s)
        self.read_maps()
        stack = (path / "ice.stk").read_text()
        match = re.search(r"heat transfer coefficient\s+([0-9.eE+-]+)", stack)
        require(match is not None, "Expected a heat-transfer coefficient in the saved stack")
        self.h = float(match.group(1)) * 1e12  # W/um²/K -> W/m²/K.
        self.metrics = self.make_metrics()
        print(f"Validated {self.n:,} intervals, {len(self.paths)} components and {len(self.regions)} regions", flush=True)

    def check(self, name, actual, expected, atol=1e-8, rtol=1e-9):
        error = close(actual, expected, f"{self.path.name}: {name} mismatch", atol, rtol)
        self.validation.append({"profile": self.profile, "check": name, "maximum_absolute_error": error,
                                "absolute_tolerance": atol, "relative_tolerance": rtol, "result": "PASS"})

    def read_die_series(self):
        rows = np.atleast_1d(np.genfromtxt(self.path / "die_timeseries.csv", delimiter=",", names=True,
                                         dtype=None, encoding="utf-8"))
        require(rows.dtype.names == ("time_ps", "die", "dynamic_w", "leakage_w", "constant_w", "peak_component_c"),
                "Per-die timeseries columns differ")
        require(len(rows) == self.n * len(self.die_groups), "Per-die timeseries row count differs")
        for die in self.die_groups:
            selected = rows[rows["die"] == die]
            require(np.array_equal(selected["time_ps"], self.time_ps), "Per-die time grid mismatch")
            for field in ("dynamic", "leakage", "constant"):
                self.check(f"{die}_{field}_power_series_w", selected[field + "_w"], self.die_power[die][field])
            self.check(f"{die}_peak_series_c", selected["peak_component_c"], self.die_peak_c[die][1:])

    def read_temperatures(self):
        self.peak_c = np.empty(self.n + 1)
        self.kind_peak_c = np.empty((self.n + 1, len(self.kinds)))
        self.die_peak_c = {die: np.empty(self.n + 1) for die in self.die_groups}
        self.component_peak_c = np.full(len(self.paths), -np.inf)
        self.component_peak_time_s = np.zeros(len(self.paths))
        self.frame_temperatures = {}
        count = 0
        for start, data in wide_chunks(self.path / "component_temperatures_c.csv.gz", self.paths):
            end = start + len(data)
            require(end <= self.n + 1, "Too many temperature rows")
            require(np.array_equal(data[:, 0], np.arange(start, end) * self.slot_ps), "Temperature time grid mismatch")
            values = data[:, 1:]
            self.peak_c[start:end] = values.max(axis=1)
            for i, group in enumerate(self.groups):
                self.kind_peak_c[start:end, i] = values[:, group].max(axis=1)
            for die, group in self.die_groups.items():
                self.die_peak_c[die][start:end] = values[:, group].max(axis=1)
            maxima = values.max(axis=0)
            changed = maxima > self.component_peak_c
            self.component_peak_time_s[changed] = (values.argmax(axis=0)[changed] + start) * self.slot_s
            self.component_peak_c = np.maximum(self.component_peak_c, maxima)
            if start == 0:
                self.initial_c = values[0].copy()
            if end == self.n + 1:
                self.check("final_component_temperatures_c", values[-1], self.final_c)
            for index in self.frame_indices[(self.frame_indices + 1 >= start) & (self.frame_indices + 1 < end)]:
                self.frame_temperatures[int(index)] = values[index + 1 - start].copy()
            count = end
        require(count == self.n + 1, "Truncated temperature history")
        self.check("peak_temperature_series_c", self.peak_c[1:], self.series["peak_component_c"])
        self.check("peak_component_c", self.peak_c.max(), self.summary["peak_component_c"])
        self.check("final_peak_component_c", self.peak_c[-1], self.summary["final_peak_component_c"])
        self.representatives = np.array([g[self.component_peak_c[g].argmax()] for g in self.groups])
        # A second pass keeps only the chosen fixed instances, not the full matrix.
        self.representative_c = np.empty((self.n + 1, len(self.kinds)))
        for start, data in wide_chunks(self.path / "component_temperatures_c.csv.gz", self.paths,
                                       usecols=np.r_[0, self.representatives + 1]):
            self.representative_c[start:start + len(data)] = data[:, 1:]

    def read_power(self, field):
        print(f"  Integrating {field} power", flush=True)
        energy = np.zeros(len(self.paths))
        grouped = np.empty((self.n, len(self.kinds)))
        checksum = hashlib.sha256()
        dram_checksum = hashlib.sha256()
        for die in self.die_groups:
            self.die_power[die][field] = np.empty(self.n)
        maximum_total_error, maximum_constant_error = 0., 0.
        if field == "leakage":
            self.representative_leakage_w = np.empty((self.n, len(self.kinds)))
        count = 0
        for start, data in wide_chunks(self.path / (field + "_power_w.csv.gz"), self.paths):
            end = start + len(data)
            require(end <= self.n and np.array_equal(data[:, 0], self.time_ps[start:end]), "Power time grid mismatch")
            values = data[:, 1:]
            require((values >= 0).all(), "Negative component power")
            checksum.update(np.ascontiguousarray(values, dtype="<f8").tobytes())
            maximum_total_error = max(maximum_total_error, close(values.sum(axis=1), self.series[field + "_w"][start:end],
                                                               f"{field} power totals differ"))
            energy += values.sum(axis=0) * self.slot_s
            for i, group in enumerate(self.groups):
                grouped[start:end, i] = values[:, group].sum(axis=1)
            for die, group in self.die_groups.items():
                self.die_power[die][field][start:end] = values[:, group].sum(axis=1)
            if field == "leakage":
                dram_checksum.update(np.ascontiguousarray(values[:, ~self.logic], dtype="<f8").tobytes())
                self.representative_leakage_w[start:end] = values[:, self.representatives]
                if self.profile == "constant":
                    if start == 0:
                        reference = values[0, self.logic].copy()
                    if self.logic.any():
                        maximum_constant_error = max(maximum_constant_error,
                            close(values[:, self.logic], reference, "Constant logic leakage changed"))
            count = end
        require(count == self.n, f"Truncated {field} history")
        self.check(field + "_power_total_w", maximum_total_error, 0.)
        self.check(field + "_energy_j", energy.sum(), self.summary[field + "_energy_j"])
        self.check(field + "_component_energy_j", energy, self.final_energy[field], atol=1e-9, rtol=1e-8)
        if field == "dynamic":
            self.dynamic_sha256 = checksum.hexdigest()
        elif self.profile == "constant":
            self.check("constant_leakage_w", maximum_constant_error, 0.)
        if field == "leakage":
            self.dram_background_sha256 = dram_checksum.hexdigest()
        self.energy[field], self.kind_power[field] = energy, grouped

    def read_maps(self):
        print("  Reading floorplan temperatures", flush=True)
        names, frames = [], []
        self.regions, self.region_groups = [], {}
        self.map_min_c, self.map_max_c = math.inf, -math.inf
        wanted = set(self.frame_indices)
        for die in self.layout.dies:
            die_names, stored, count = None, {}, 0
            with (self.path / die["temperature_output"]).open() as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    if line.lstrip().startswith("%"):
                        die_names = VIEW["parse_tflp_header"](line) or die_names
                        continue
                    require(die_names == die["elements"], f"Wrong native floorplan header for {die['id']}")
                    row = np.fromstring(line, sep=" ")
                    require(row.size == len(die_names) + 1 and np.isfinite(row).all(), "Invalid native map row")
                    # Native timestamps are printed to milliseconds; index is authoritative.
                    close(row[0], (count + 1) * self.slot_s, "Native map timestamp mismatch", atol=.000501, rtol=0.)
                    values = row[1:] - 273.15
                    self.map_min_c = min(self.map_min_c, float(values.min()))
                    self.map_max_c = max(self.map_max_c, float(values.max()))
                    if count in wanted:
                        stored[count] = values.copy()
                    count += 1
            require(count == self.n and len(stored) == len(wanted), "Native map row count mismatch")
            regions = VIEW["align_regions_to_tflp"](die["floorplan"],
                          VIEW["load_floorplan_regions"](die["floorplan"]), die_names)
            if self.layout.multidie:
                regions = [dict(region, die=die["id"]) for region in regions]
            self.region_groups[die["id"]] = np.arange(len(names), len(names) + len(die_names))
            self.regions.extend(regions)
            names.extend(die_names)
            frames.append(np.array([stored[int(i)] for i in self.frame_indices]))
        require(len(set(names)) == len(names), "Duplicate native map columns")
        self.map_frames = np.concatenate(frames, axis=1)
        by_name = {name: i for i, name in enumerate(names)}
        mapped = np.empty((len(self.frame_indices), len(self.paths)))
        for i, entry in enumerate(self.mapping):
            indices = [by_name[name.strip("/").replace("/", "__")] for name in entry["floorplan_elements"]]
            areas = [self.regions[j]["width"] * self.regions[j]["height"] for j in indices]
            mapped[:, i] = np.average(self.map_frames[:, indices], weights=areas, axis=1)
        expected = np.array([self.frame_temperatures[int(i)] for i in self.frame_indices])
        # Tflp text rounds double-precision kelvin values to 0.001 K. The
        # independently serialized socket history rounds them to float32 first.
        kelvin = np.float32(max(self.map_max_c, float(expected.max())) + 273.15)
        self.map_tolerance_c = .0005 + float(np.spacing(kelvin)) / 2 + 1e-9
        self.check("sampled_floorplan_component_temperatures_c", mapped, expected, atol=self.map_tolerance_c, rtol=0.)
        del self.frame_temperatures

    def make_metrics(self):
        s = self.summary
        peak_index = int(self.peak_c.argmax())
        hottest = int(self.component_peak_c.argmax())
        late = self.time_s > max(0., self.duration - .1) + 1e-12
        return {"profile": self.profile, "run": str(self.path), "status": s["status"],
                "simulated_seconds": self.duration, "host_minutes": s["host_seconds"] / 60,
                "slots": self.n, "thermal_step_us": s["step_ps"] * 1e-6,
                "power_slot_us": self.slot_ps * 1e-6, "h_w_m2_k": self.h,
                "initial_temperature_c": float(self.initial_c.max()),
                "peak_component_c": float(self.peak_c.max()), "peak_time_s": peak_index * self.slot_s,
                "peak_rise_c": float(self.peak_c.max() - self.initial_c.max()),
                "hottest_component": self.paths[hottest], "final_peak_component_c": float(self.peak_c[-1]),
                "mean_dynamic_w": float(self.energy["dynamic"].sum() / self.duration),
                "mean_leakage_w": float(self.energy["leakage"].sum() / self.duration),
                "mean_total_w": float(self.total_w.mean()),
                "dynamic_energy_j": float(self.energy["dynamic"].sum()),
                "leakage_energy_j": float(self.energy["leakage"].sum()),
                "total_energy_j": float(self.total_w.sum() * self.slot_s),
                "leakage_energy_percent": 100 * float(self.energy["leakage"].sum()) / s["total_energy_j"],
                "last_100ms_mean_leakage_w": float(self.series["leakage_w"][late].mean()),
                "last_100ms_mean_dynamic_w": float(self.series["dynamic_w"][late].mean()),
                "cycles_completed": s["cycles_completed"], "layers_completed": s["layers_completed"],
                "layers_per_cycle": s["layers_per_cycle"],
                "component_dynamic_trace_sha256": self.dynamic_sha256}


def validate_pair(constant, aware):
    require(constant.paths == aware.paths and constant.mapping == aware.mapping, "A/B component mapping mismatch")
    for key in ("recording_sha256", "server_sha256", "step_ps", "slot_ps", "period_ps", "layer_start_ps",
                "simulated_seconds", "slots", "factorization_threads", "layers_per_cycle"):
        require(constant.summary[key] == aware.summary[key], f"A/B {key} mismatch")
    for filename in ("ice.stk", *(p.name for p in constant.layout.inputs)):
        require((constant.path / filename).read_bytes() == (aware.path / filename).read_bytes(), f"A/B {filename} mismatch")
    require(constant.regions == aware.regions, "A/B floorplan ordering mismatch")
    require(constant.dynamic_sha256 == aware.dynamic_sha256, "A/B component dynamic traces differ")
    require(constant.dram_background_sha256 == aware.dram_background_sha256, "A/B DRAM background traces differ")
    require(np.array_equal(constant.frame_indices, aware.frame_indices), "A/B animation timestamps differ")
    close(constant.initial_c, aware.initial_c, "A/B initial temperatures differ")


def configure_style():
    # Match experiments/leakage_temperature/analyze.py and the LLM decode report.
    plt.rcParams.update({"font.family": "serif", "font.serif": ["DejaVu Serif"],
                         "mathtext.fontset": "dejavuserif", "font.size": 10.5,
                         "axes.titlesize": 11.5, "axes.labelsize": 10.5, "legend.fontsize": 9.2,
                         "axes.spines.top": False, "axes.spines.right": False,
                         "axes.grid": True, "grid.alpha": .24, "grid.linewidth": .7,
                         "figure.dpi": 120, "savefig.dpi": 320, "savefig.bbox": "tight"})


class Figures:
    def __init__(self, output, constant, aware):
        self.output, self.constant, self.aware = output, constant, aware
        self.items = []
        source = Path(aware.summary["recording"]) / "source_workload.json"
        workload = json.loads(source.read_text()) if source.is_file() else {}
        model = workload.get("llm_model", "thermal")
        batch = re.search(r"_b(\d+)_(?:constant|temperature_aware)$", Path(aware.manifest["source_run"]).name)
        self.prefix = re.sub(r"[^A-Za-z0-9_.-]", "_", model + (f"_b{batch[1]}" if batch else "_replay"))
        self.workload = f"{model} · batch {batch[1]}" if batch else model
        self.title = f"{self.workload} replay · h = {aware.h:,.0f} W/(m²·K)"
        self.vmin = 5 * math.floor(min(r.map_min_c for r in (constant, aware)) / 5)
        self.vmax = max(self.vmin + 5, 5 * math.ceil(max(r.map_max_c for r in (constant, aware)) / 5))

    def save(self, fig, name, caption):
        for suffix in ("png", "pdf"):
            fig.savefig(self.output / f"{name}.{suffix}")
        plt.close(fig)
        self.items.append({"name": name, "caption": caption})
        print("Saved", name, flush=True)

    def comparison(self):
        fig, axes = plt.subplots(1, 4, figsize=(20, 4.1), layout="constrained")
        for run, color in ((self.constant, BLUE), (self.aware, ORANGE)):
            label = PROFILE_LABELS[run.profile]
            edges = np.r_[0., run.time_s]
            temperature_line, = axes[0].plot(edges, run.peak_c, color=color, lw=.8,
                                             label=f"{label}: {run.peak_c.max():.2f} °C peak")
            temperature_line.get_path().should_simplify = False
            # Each value applies to the interval ending at time_s[i].
            line, = axes[1].step(edges, np.r_[run.total_w[0], run.total_w],
                                 where="pre", color=color, lw=.55)
            line.get_path().should_simplify = False
        power_max = max(float(run.series[field].max()) for run in (self.constant, self.aware)
                        for field in ("dynamic_w", "leakage_w"))
        for axis, run in zip(axes[2:], (self.constant, self.aware)):
            edges = np.r_[0., run.time_s]
            for field, label, color, width in (("dynamic_w", "Dynamic", "#009E73", .55),
                                                ("leakage_w", "Leakage", "#7B3294", .9)):
                power = run.series[field]
                line, = axis.step(edges, np.r_[power[0], power], where="pre",
                                   color=color, lw=width, label=label)
                line.get_path().should_simplify = False
            axis.set_ylim(0, 1.15 * power_max if power_max > 0 else 1.)
            axis.legend(fontsize=8, loc="upper right")
        axes[2].set_title("Constant-leakage run")
        axes[3].set_title("Temperature-aware run")
        for ax, ylabel in zip(axes, ["Hottest component average (°C)", "Total modeled power (W)",
                                    "Power (W)", "Power (W)"]):
            ax.set(xlabel="Simulation time (s)", ylabel=ylabel, xlim=(0, self.aware.duration))
        axes[0].legend(fontsize=8, loc="lower right")
        slot_us = self.aware.slot_ps * 1e-6
        fig.suptitle(self.title + f"\nIdentical dynamic activity · all saved {slot_us:g} µs samples")
        self.save(fig, self.prefix + "_comparison", f"Constant and temperature-aware profiles over the complete replay. "
                  "The third and fourth panels show dynamic (green) and leakage (purple) power for the "
                  "constant-leakage and temperature-aware runs, respectively, with a shared power scale. "
                  f"Power uses a step curve for every saved {slot_us:g} µs interval; temperature uses every saved endpoint. "
                  "No display averaging or shaded bands.")

    def leakage_temperature(self):
        run = self.aware
        kinds = [k for k in run.kinds if k != "transpose"]
        rows = math.ceil(len(kinds) / 3)
        fig, axes = plt.subplots(rows, 3, squeeze=False, figsize=(15, 4.15 * rows), layout="constrained")
        for kind, ax in zip(kinds, axes.flat):
            i = run.kinds.index(kind)
            right = ax.twinx()
            envelope(ax, run, run.representative_leakage_w[:, i], BLUE, scale=1000)
            envelope(right, run, run.representative_c[1:, i], ORANGE)
            ax.set(xlabel="Simulation time (s)", ylabel="Background (mW)" if kind == "dram3d_channel" else "Leakage (mW)",
                   title=LABELS[kind], xlim=(0, run.duration))
            right.set_ylabel("Temperature (°C)", color=ORANGE)
            ax.tick_params(axis="y", labelcolor=BLUE)
            right.tick_params(axis="y", labelcolor=ORANGE)
            right.grid(False)
            ax.text(.025, .965, run.paths[run.representatives[i]].replace("/chip/", ""), transform=ax.transAxes, va="top", fontsize=8)
        for ax in list(axes.flat)[len(kinds):]:
            ax.set_visible(False)
        fig.suptitle(self.title + "\nTemperature-aware: hottest fixed instance of each kind · 1 ms means and min/max envelopes")
        self.save(fig, self.prefix + "_leakage_temperature_time", "Blue: interval logic leakage or DRAM background power, left axis. Orange: endpoint temperature, right axis. Each panel follows one fixed component. Logic leakage uses the preceding slot's temperature; DRAM background retains its recorded state dependence and fixed electrical characterization.")

    def workload_summary(self):
        c, a = self.constant, self.aware
        fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.8), layout="constrained")
        bottom = 0.
        for kind, group in zip(a.kinds, a.groups):
            value = float(a.energy["dynamic"][group].sum() / a.duration)
            axes[0].bar(0, value, bottom=bottom, width=.55, label=LABELS[kind], color=COLORS[kind])
            bottom += value
        axes[0].set(xticks=[0], xticklabels=[self.workload], ylabel="Mean dynamic power (W)",
                    title="Activity + residual clocks by component", xlim=(-.9, .9), ylim=(0, bottom * 1.38))
        axes[0].legend(ncol=2, fontsize=8, loc="upper left")
        axes[0].annotate(f"{bottom:.3f} W", (0, bottom), xytext=(0, 5), textcoords="offset points",
                         ha="center", fontsize=9)
        values = [100 * (a.metrics[k] / c.metrics[k] - 1) for k in ("leakage_energy_j", "total_energy_j")]
        axes[1].bar([-.18, .18], values, .32, color=[ORANGE, BLUE])
        axes[1].set(xticks=[-.18, .18], xticklabels=["Leakage energy", "Total energy"],
                    ylabel="Increase over constant leakage (%)", title="Temperature-aware energy impact", ylim=(0, max(values) * 1.17))
        for x, value in zip([-.18, .18], values):
            axes[1].text(x, value + max(values) * .018, f"+{value:.1f}%", ha="center")
        fig.suptitle(self.title + f"\nEnergy integrated over all {a.n:,} intervals ({a.duration:g} s)")
        self.save(fig, "workload_summary", "All recorded component kinds contribute to the dynamic-power stack, including DRAM commands when present. Percentage changes use full-resolution integrated energy.")

    def map_panel(self, ax, values, cmap="inferno", limits=None, die=None):
        if self.aware.layout.multidie and die is None:
            raise ValueError("select a die before plotting overlapping footprints")
        indices = self.aware.region_groups[die] if die else np.arange(len(self.aware.regions))
        regions = [self.aware.regions[i] for i in indices]
        patches = [Rectangle((r["x"] / 1000, r["y"] / 1000), r["width"] / 1000, r["height"] / 1000)
                   for r in regions]
        coll = PatchCollection(patches, cmap=cmap, edgecolors="none")
        coll.set_array(values[indices])
        coll.set_clim(*(limits or (self.vmin, self.vmax)))
        ax.add_collection(coll)
        bounds = VIEW["floorplan_bounds"](regions)
        ax.set(xlim=(bounds["minX"] / 1000, bounds["maxX"] / 1000), ylim=(bounds["minY"] / 1000, bounds["maxY"] / 1000),
               aspect="equal", xlabel="x (mm)", ylabel="y (mm)")
        ax.grid(False)
        return coll

    def thermal_maps(self):
        for die in self.aware.layout.dies:
            self.thermal_map(die["id"] if self.aware.layout.multidie else None)

    def thermal_map(self, die):
        fig, axes = plt.subplots(1, 3, figsize=(13.2, 4.7), layout="constrained")
        indices = self.aware.region_groups[die] if die else np.arange(len(self.aware.regions))
        for ax, run in zip(axes, (self.constant, self.aware)):
            coll = self.map_panel(ax, run.map_frames[-1], die=die)
            ax.set_title(PROFILE_LABELS[run.profile] + f"\nPeak region: {run.map_frames[-1, indices].max():.2f} °C")
        fig.colorbar(coll, ax=list(axes[:2]), label="Floorplan-average temperature (°C)", shrink=.78, pad=.025)
        delta = self.aware.map_frames[-1] - self.constant.map_frames[-1]
        maximum = max(1e-6, float(np.abs(delta[indices]).max()))
        negative = delta[indices].min() < 0
        coll = self.map_panel(axes[2], delta, cmap="coolwarm" if negative else "magma", die=die,
                              limits=(-maximum, maximum) if negative else (0, maximum))
        axes[2].set_title("Temperature-aware − constant")
        fig.colorbar(coll, ax=axes[2], label="Temperature difference (°C)", shrink=.78, pad=.025)
        fig.suptitle(self.title + (f" · {die}" if die else "") + f"\nFinal floorplan averages at {self.aware.duration:g} s; absolute maps share the GIF scale")
        self.save(fig, self.prefix + (f"_{die.lower()}" if die else "") + "_thermal_maps", "Final saved floorplan-region averages and their difference; these maps do not resolve within-region thermal-cell gradients.")

    def floorplan(self):
        run = self.aware
        if run.layout.multidie:
            return self.die_floorplans()
        cluster = run.document["geometry"]["chip"]["subs"]["cluster_0"]
        kind_by_name = {name.rsplit("/", 1)[-1]: run.inventory[entry["path"]]["kind"]
                        for entry in run.mapping for name in entry["floorplan_elements"] if name.startswith("chip/cluster_0/")}
        fig, (ax, legend) = plt.subplots(1, 2, figsize=(9, 7), gridspec_kw={"width_ratios": [1.6, 1]}, layout="constrained")
        callouts = []
        short = {"redmule": "RedMulE", "tcdm": "TCDM", "instr_mem": "Instr.\nSRAM", "stack_mem": "Stack\nSRAM",
                 "data_noc_router": "Data\nrouter", "data_noc_ni": "NI", "idma": "iDMA"}
        for name, region in cluster["subs"].items():
            x, y = np.array(region["offset"]) / 1000
            width, height = np.array(region["shape"]) / 1000
            ax.add_patch(Rectangle((x, y), width, height, facecolor=COLORS.get(kind_by_name.get(name), "#eeeeee"), edgecolor="white", alpha=.8))
            rotate = width < .065 and height > .15
            label_y = y + height / 2
            if min(width, height) < .035 and not rotate:
                callouts.append(name)
                label = str(len(callouts))
                label_y = y + height * (.18 + .2 * ((len(callouts) - 1) % 4))
            else:
                label = short.get(name, name)
            ax.text(x + width / 2, label_y, label, ha="center", va="center", fontsize=8, rotation=90 if rotate else 0)
        ax.set(xlim=(0, cluster["shape"][0] / 1000), ylim=(0, cluster["shape"][1] / 1000),
               aspect="equal", xlabel="x (mm)", ylabel="y (mm)")
        ax.grid(False)
        legend.axis("off")
        legend.text(0, .95, "Small regions", va="top", weight="bold", transform=legend.transAxes)
        for i, name in enumerate(callouts):
            legend.text(0, .9 - i * .045, f"{i+1}. {name.replace('_', ' ')}", va="top", transform=legend.transAxes, fontsize=9)
        top = min(.58, .85 - len(callouts) * .045)
        for i, kind in enumerate(run.kinds):
            y = top - i * .05
            legend.add_patch(Rectangle((0, y), .07, .025, transform=legend.transAxes, color=COLORS[kind]))
            legend.text(.1, y + .012, LABELS[kind], va="center", transform=legend.transAxes)
        count = run.document["metadata"]["cluster_grid"]["count"]
        legend.text(0, top - .39, f"Area-estimated layout\nNot placement and routing\n\n{count} identical clusters", va="top", transform=legend.transAxes, fontsize=9)
        fig.suptitle("Expanded cluster floorplan — " + run.document["metadata"]["floorplan_rule"])
        self.save(fig, "cluster_floorplan", "Cluster 0 from the archived geometry, colored by component kind; other regions appear gray.")

    def die_floorplans(self):
        run = self.aware
        kinds = {name.strip("/").replace("/", "__"): run.inventory[entry["path"]]["kind"]
                 for entry in run.mapping for name in entry["floorplan_elements"]}
        for die, indices in run.region_groups.items():
            fig, ax = plt.subplots(figsize=(8, 7), layout="constrained")
            regions = [run.regions[i] for i in indices]
            patches = [Rectangle((r["x"] / 1000, r["y"] / 1000), r["width"] / 1000, r["height"] / 1000)
                       for r in regions]
            colors = [COLORS.get(kinds.get(r["name"]), "#eeeeee") for r in regions]
            ax.add_collection(PatchCollection(patches, facecolors=colors, edgecolors="white", linewidths=.3))
            bounds = VIEW["floorplan_bounds"](regions)
            ax.set(xlim=(bounds["minX"] / 1000, bounds["maxX"] / 1000),
                   ylim=(bounds["minY"] / 1000, bounds["maxY"] / 1000),
                   aspect="equal", xlabel="x (mm)", ylabel="y (mm)", title=die)
            ax.grid(False)
            present = {kinds.get(r["name"]) for r in regions}
            ax.legend(handles=[Rectangle((0, 0), 1, 1, color=COLORS[k], label=LABELS[k])
                               for k in run.kinds if k in present], loc="upper center", bbox_to_anchor=(.5, -.12), ncol=3)
            self.save(fig, die.lower() + "_floorplan", "Archived die geometry colored by component kind; fixed-power filler is gray. Each die is plotted separately.")

    def die_comparisons(self):
        if not self.aware.layout.multidie:
            return
        for die in self.aware.die_groups:
            fig, axes = plt.subplots(1, 3, figsize=(15, 4.2), layout="constrained")
            for run, color in ((self.constant, BLUE), (self.aware, ORANGE)):
                edges = np.r_[0., run.time_s]
                powers = run.die_power[die]
                axes[0].plot(edges, run.die_peak_c[die], color=color, label=PROFILE_LABELS[run.profile])
                for ax, field in zip(axes[1:], ("dynamic", "leakage")):
                    ax.step(edges, np.r_[powers[field][0], powers[field]], where="pre", color=color)
            axes[0].legend(fontsize=8)
            dram = die == "DRAM_DIE"
            for ax, label in zip(axes, ("Peak component (°C)", "Commands (W)" if dram else "Dynamic power (W)",
                                       "Background (W)" if dram else "Leakage (W)")):
                ax.set(xlabel="Simulation time (s)", ylabel=label, xlim=(0, self.aware.duration))
            fig.suptitle(self.title + " · " + die)
            self.save(fig, die.lower() + "_comparison", "Per-die power and temperature histories. DRAM command and state-dependent background traces are identical between profiles; only logic leakage depends on temperature.")

    def assumptions(self):
        run = self.aware
        metadata = run.document["metadata"]
        model = LeakageModel(run.document, run.paths, run.manifest["temperature_grid_c"])
        fig, (ax, table_ax) = plt.subplots(1, 2, figsize=(11.5, 4.8), layout="constrained")
        for kind, label in (("light_redmule", "Logic / RedMulE"), ("memory", "SRAM"),
                            ("dram3d_channel", "DRAM: fixed electrical model")):
            if kind not in run.kinds:
                continue
            index = int(run.groups[run.kinds.index(kind)][0])
            ax.plot(model.grid, model.table[:, index], "o-", ms=4, color=COLORS[kind], label=label)
        ax.axvspan(run.initial_c.max(), run.peak_c.max(), color=ORANGE, alpha=.08, label="Aware run: initial to peak")
        ax.set(xlabel="Temperature (°C)", ylabel="Leakage / leakage at 25 °C", title="Archived sampled leakage model")
        ax.legend(loc="upper left", fontsize=8)
        table_ax.axis("off")
        rows = [["Technology / voltage", f"{metadata['technology_node']} / {metadata['power_voltage_v']:g} V"],
                ["Cooling coefficient", f"{run.h:,.0f} W/(m²·K)"],
                ["Initial temperature", f"{run.initial_c.max():.2f} °C"], ["Fixed-leakage reference", "25 °C"],
                ["Thermal step / power slot", f"{run.summary['step_ps'] * 1e-6:g} / {run.slot_ps * 1e-6:g} µs"],
                ["Saved duration", f"{run.duration:g} s"], ["Layers per replay block", str(run.summary["layers_per_cycle"])],
                ["Component power domains", str(len(run.paths))], ["Thermal cells", str(run.summary["thermal_cells"])]]
        table = table_ax.table(cellText=rows, colWidths=[.53, .47], cellLoc="left", bbox=[0, .16, 1, .79])
        table.auto_set_font_size(False)
        table.set_fontsize(9)
        for (row, col), cell in table.get_celld().items():
            cell.set_edgecolor("#dddddd")
            if row % 2 == 0:
                cell.set_facecolor("#f4f4f4")
        table_ax.text(0, .06, "Linear interpolation between archived samples.\nCharacterization ends at 125 °C.", fontsize=9, transform=table_ax.transAxes)
        fig.suptitle(self.title + "\nModel assumptions and actual replay settings")
        self.save(fig, "model_assumptions", "Leakage factors come from the recording's archived tables at the selected technology. No model parameters are copied from the older reference experiment.")

    def animation(self, run, die=None):
        name = self.prefix + "_" + run.profile + ("_" + die.lower() if die else "") + ".gif"
        fig = plt.figure(figsize=(10, 8), dpi=100)
        ax = fig.add_axes([.10, .27, .72, .60])
        color_ax = fig.add_axes([.84, .30, .022, .54])
        coll = self.map_panel(ax, run.map_frames[0], cmap="turbo", die=die)
        fig.colorbar(coll, cax=color_ax, label="Floorplan-average temperature (°C)")
        fig.text(.5, .96, self.title, ha="center", fontsize=12)
        fig.text(.5, .921, PROFILE_LABELS[run.profile] + (" · " + die if die else ""), ha="center", fontsize=11)
        time_label = fig.text(.5, .885, "", ha="center", fontsize=10)
        indices = run.region_groups[die] if die else np.arange(len(run.regions))
        peak = run.die_peak_c[die] if die else run.peak_c
        curve = fig.add_axes([.12, .105, .74, .10])
        envelope(curve, run, peak[1:], BLUE if run.profile == "constant" else ORANGE)
        cursor = curve.axvline(run.time_s[0], color="#333333", lw=1.4)
        curve.set(xlim=(0, run.duration), ylim=(self.vmin, self.vmax), xlabel="Simulation time (s)", ylabel="Peak (°C)")
        fig.text(.5, .018, "Shared temperature scale · saved floorplan averages · frames every 10 ms, plus endpoints", ha="center", fontsize=9)

        def animate(frame):
            index = int(run.frame_indices[frame])
            coll.set_array(run.map_frames[frame, indices])
            time_label.set_text(f"Time: {run.time_s[index]:.5f} s     Hottest component: {peak[index + 1]:.2f} °C")
            cursor.set_xdata([run.time_s[index]] * 2)
            return coll, time_label, cursor

        animation = FuncAnimation(fig, animate, frames=len(run.frame_indices), interval=100, blit=False)
        print(f"Rendering {name}: {len(run.frame_indices)} frames", flush=True)
        with plt.rc_context({"savefig.bbox": None}):
            animation.save(self.output / name, writer=PillowWriter(fps=10), dpi=100)
        plt.close(fig)
        return name


def write_data(output, runs):
    component_rows, kind_rows, time_rows, cycle_rows = [], [], [], []
    for run in runs:
        for i, path in enumerate(run.paths):
            dynamic, leakage = float(run.energy["dynamic"][i]), float(run.energy["leakage"][i])
            component_rows.append({"profile": run.profile, "path": path, "kind": run.inventory[path]["kind"],
                                   "dynamic_energy_j": dynamic, "leakage_energy_j": leakage, "total_energy_j": dynamic + leakage,
                                   "peak_temperature_c": float(run.component_peak_c[i]),
                                   "peak_time_s": float(run.component_peak_time_s[i]), "final_temperature_c": float(run.final_c[i])})
        for i, (kind, group) in enumerate(zip(run.kinds, run.groups)):
            dynamic, leakage = (float(run.energy[k][group].sum()) for k in ("dynamic", "leakage"))
            kind_rows.append({"profile": run.profile, "kind": kind, "instances": len(group),
                              "dynamic_energy_j": dynamic, "leakage_energy_j": leakage, "total_energy_j": dynamic + leakage,
                              "mean_dynamic_w": dynamic / run.duration, "mean_leakage_w": leakage / run.duration,
                              "peak_temperature_c": float(run.component_peak_c[group].max()),
                              "representative_path": run.paths[run.representatives[i]]})
        quantities = {"dynamic_w": run.series["dynamic_w"], "leakage_w": run.series["leakage_w"],
                      "total_w": run.total_w, "peak_component_c": run.peak_c[1:]}
        for i, kind in enumerate(run.kinds):
            quantities.update({kind + "_dynamic_w": run.kind_power["dynamic"][:, i],
                               kind + "_leakage_w": run.kind_power["leakage"][:, i],
                               kind + "_representative_leakage_w": run.representative_leakage_w[:, i],
                               kind + "_representative_c": run.representative_c[1:, i]})
        columns = {}
        for name, values in quantities.items():
            times, mean, low, high = bins(run.time_s, values, run.bin_size)
            for stat, array in (("mean", mean), ("min", low), ("max", high)):
                columns[stat + "_" + name] = array
        for i, time in enumerate(times):
            time_rows.append({"profile": run.profile, "time_s": float(time), **{key: float(v[i]) for key, v in columns.items()}})
        cycle_rows.extend({"profile": run.profile, **row} for row in run.cycles)
    for name, rows in (("metrics", [r.metrics for r in runs]), ("component_energy", component_rows),
                       ("component_kind_summary", kind_rows), ("timeseries_1ms", time_rows), ("cycles", cycle_rows),
                       ("validation", [row for r in runs for row in r.validation])):
        csv_file(output / (name + ".csv"), rows,
                 fieldnames=["profile", "cycle", "time_ps", "layers_completed"] if name == "cycles" else None)
    csv_file(output / "die_summary.csv", [{"profile": run.profile, "die": die, **values}
                                          for run in runs for die, values in run.die_metrics.items()])


def write_report(figures, gifs, command):
    c, a = figures.constant, figures.aware
    energy_increase = 100 * (a.metrics["total_energy_j"] / c.metrics["total_energy_j"] - 1)
    peak_increase = a.metrics["peak_component_c"] - c.metrics["peak_component_c"]
    summary = {"title": figures.title, "runs": {r.profile: r.metrics for r in (c, a)},
               "comparison": {"peak_temperature_increase_c": peak_increase, "total_energy_increase_percent": energy_increase},
               "pair_validation": {"identical_recording": True, "identical_effective_stack_and_floorplan": True,
                                   "identical_dynamic_power_at_every_component_and_interval": True,
                                   "identical_dram_background_at_every_component_and_interval": True},
               "per_die": {r.profile: r.die_metrics for r in (c, a)},
               "power_semantics": {"logic": "dynamic + temperature-dependent leakage",
                                   "dram": "commands + state-dependent background; fixed electrical characterization"},
               "figures": figures.items, "gifs": gifs, "gif_fps": 10, "gif_colormap": "turbo",
               "gif_frame_times_s": [float(t) for t in a.time_s[a.frame_indices]] if gifs else [],
               "map_scale_c": [figures.vmin, figures.vmax], "map_kind": "floorplan-region averages",
               "map_validation_tolerance_c": max(c.map_tolerance_c, a.map_tolerance_c),
               "display_bin_seconds": a.bin_size * a.slot_s, "command": command,
               "source_sha256": {r.profile: {name: digest(r.path / name) for name in
                                  ("summary.json", "components.json", "ice.stk", "final_state.npz",
                                   *(p.name for p in r.layout.inputs))} for r in (c, a)}}
    write_json(figures.output / "analysis_summary.json", summary)
    rows = [("Duration (s)", "simulated_seconds", ".3f"), ("Peak component (°C)", "peak_component_c", ".3f"),
            ("Temperature rise (°C)", "peak_rise_c", ".3f"), ("Hottest component at end (°C)", "final_peak_component_c", ".3f"),
            ("Mean dynamic power (W)", "mean_dynamic_w", ".4f"), ("Mean leakage power (W)", "mean_leakage_w", ".4f"),
            ("Total energy (J)", "total_energy_j", ".5f"), ("Leakage share of energy (%)", "leakage_energy_percent", ".2f"),
            ("Completed blocks", "cycles_completed", "d"), ("Host time (min)", "host_minutes", ".2f")]
    table = "\n".join(f"<tr><th>{label}</th><td>{c.metrics[key]:{fmt}}</td><td>{a.metrics[key]:{fmt}}</td></tr>" for label, key, fmt in rows)
    cards = "\n".join(f'<section><h2>{html.escape(item["name"].replace(figures.prefix + "_", "").replace("_", " ").title())}</h2>'
                       f'<p>{html.escape(item["caption"])}</p><p><a href="{item["name"]}.png">PNG</a> · '
                       f'<a href="{item["name"]}.pdf">PDF</a></p><img loading="lazy" src="{item["name"]}.png" '
                       f'alt="{html.escape(item["caption"])}"></section>' for item in figures.items)
    animations = "\n".join(f'<section><h2>{html.escape(PROFILE_LABELS.get(profile, profile.replace("/", " · ")))} animation</h2><p><a href="{name}">Open GIF</a>'
                            f' · {len(a.frame_indices)} frames · 10 frames/s · {figures.vmin:g}–{figures.vmax:g} °C</p>'
                            f'<img loading="lazy" src="{name}" alt="{html.escape(profile)} floorplan animation"></section>'
                            for profile, name in gifs.items())
    links = " · ".join(f'<a href="{name}.csv">{label}</a>' for name, label in
                       (("metrics", "Metrics"), ("component_energy", "Component energy"), ("component_kind_summary", "Kind summaries"),
                        ("die_summary", "Per-die energy and temperature"), ("timeseries_1ms", "Plot data"),
                        ("cycles", "Replay boundaries"), ("validation", "Validation")))
    die_note = ("Logic leakage depends on temperature. DRAM command and state-dependent background power are replayed "
                "at their archived electrical characterization, with no temperature multiplier. In aggregate metrics, "
                "dynamic includes DRAM commands and leakage includes DRAM background. The per-die CSV and figures "
                "separate them. All die maps and GIFs share one temperature scale." if a.layout.multidie else "")
    states = "; ".join(f'{PROFILE_LABELS[r.profile]}: <code>{r.summary["status"]}</code>' for r in (c, a))
    report = f'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(figures.title)}</title>
<style>body{{font:16px/1.55 system-ui,sans-serif;color:#20242a;background:#f6f7f9;max-width:1250px;margin:32px auto;padding:0 24px}}
h1{{font-size:27px}}h2{{font-size:21px}}section{{background:white;padding:20px 24px;margin:24px 0;border:1px solid #dfe3e8;border-radius:8px}}
img{{display:block;max-width:100%;height:auto;margin:14px auto}}table{{border-collapse:collapse;width:100%}}th,td{{text-align:left;border-bottom:1px solid #ddd;padding:7px 12px}}
a{{color:#0066a2}}code,pre{{font-size:13px;overflow-wrap:anywhere}}pre{{white-space:pre-wrap}}.note{{border-left:4px solid #d55e00;padding-left:14px}}</style>
<h1>{html.escape(figures.title)}</h1>
<p>Paired saved replays: {a.duration:g} s, thermal step {a.summary['step_ps'] * 1e-6:g} µs, power slot {a.slot_ps * 1e-6:g} µs.
The same recorded {a.summary['layers_per_cycle']}-layer block repeats with continuous thermal state.</p>
<section><h2>Results</h2><table><tr><th>Metric</th><th>Constant leakage</th><th>Temperature-aware</th></tr>{table}</table>
<p class="note">Temperature-aware leakage raises the sampled component peak by {peak_increase:.2f} °C and total energy by {energy_increase:.1f}%.
Every saved component dynamic-power sample is identical between profiles, so the energy difference comes from leakage.</p>
<p>{states}. Temperatures and energies cover the saved simulation interval.</p><p>{die_note}</p></section>
{cards}{animations}
<section><h2>Data and validation</h2><p>{links} · <a href="analysis_summary.json">Analysis JSON</a></p>
<p>Metrics and integration use every saved {a.slot_ps * 1e-6:g} µs interval. The comparison figure plots every saved sample without display averaging or shaded bands.
Other time curves use {a.bin_size * a.slot_s * 1000:g} ms display bins with sampled minimum/maximum envelopes.
Representative leakage and temperature curves follow the same fixed component. Leakage describes the preceding interval and uses its starting temperature;
temperature values describe interval endpoints. All powers and energies refer to the modeled component/floorplan boundary.</p>
<p>Recording checksums, pair settings, dynamic activity, integrated energy, peak histories, final states, and sampled map-to-component temperatures were verified.
Native floorplan temperatures are rounded to 0.001 K. Validation allows half that resolution plus half a float32 kelvin ULP
for the independently rounded socket history ({max(c.map_tolerance_c, a.map_tolerance_c):.8f} °C here).
Map timestamps are reconstructed from row index × power-slot duration.</p>
<p>Maps and animations show floorplan averages. Full thermal-cell histories were not saved, so no cell-gradient animations or transient cell-maximum claims are produced.
The archived leakage model uses linear interpolation between its temperature samples and is characterized through 125 °C.
This analysis does not establish the error of the 20 µs integration step against a finer-step simulation.</p>
<p>Unmodeled in the archived contract: {html.escape('; '.join(a.document['metadata'].get('unmodeled', [])))}.</p>
<p>Constant source: <code>{html.escape(str(c.path))}</code><br>Temperature-aware source: <code>{html.escape(str(a.path))}</code><br>
Recording: <code>{html.escape(a.summary['recording'])}</code></p>
<h2>Reproduce these figures</h2><pre>{html.escape(command)}</pre></section></html>'''
    (figures.output / "report.html").write_text(report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--constant-run", type=Path, required=True)
    parser.add_argument("--aware-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Analysis directory; matching generated filenames are overwritten")
    parser.add_argument("--animations", action="store_true", help="Also render paired floorplan-average GIFs")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    for source in (args.constant_run.resolve(), args.aware_run.resolve()):
        require(output != source and source not in output.parents, "Analysis output must be outside both source runs")
        recording = Path(json.loads((source / "summary.json").read_text())["recording"]).resolve()
        require(output != recording and recording not in output.parents, "Analysis output must be outside the recording")
    constant, aware = ReplayRun(args.constant_run, "constant"), ReplayRun(args.aware_run, "temperature_aware")
    validate_pair(constant, aware)
    output.mkdir(parents=True, exist_ok=True)
    configure_style()
    figures = Figures(output, constant, aware)
    for plot in (figures.comparison, figures.die_comparisons, figures.leakage_temperature, figures.workload_summary,
                 figures.thermal_maps, figures.floorplan, figures.assumptions):
        plot()
    gifs = {}
    if args.animations:
        for run in (constant, aware):
            if run.layout.multidie:
                for die in run.layout.dies:
                    gifs[run.profile + "/" + die["id"]] = figures.animation(run, die["id"])
            else:
                gifs[run.profile] = figures.animation(run)
    write_data(output, (constant, aware))
    import shlex
    import sys
    command = shlex.join([sys.executable, "-m", "experiments.thermal_replay.analyze",
                          "--constant-run", str(constant.path), "--aware-run", str(aware.path), "--output", str(output)]
                         + (["--animations"] if args.animations else []))
    write_report(figures, gifs, command)
    print(f"Analysis complete: {output / 'report.html'}", flush=True)


if __name__ == "__main__":
    main()
