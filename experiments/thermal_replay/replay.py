"""Replay activity with continuous thermal state and explicit stopping reasons."""

from contextlib import ExitStack
import csv
import gzip
from pathlib import Path
import sys
import time

import numpy as np

from .activity import ROOT, LeakageModel, digest, load_recording, write_json
from .ice import IceClient, prepare_stack
from .layout import Layout

sys.path.insert(0, str(ROOT / "Interface_scripts/co-simulation"))
from ice_trace_adapter import build_power_sources, validate_floorplan_entries  # noqa: E402
from system_contract import (flattened_floorplan_name, geometry_entry,
                             initial_temperature_c, thermal_components)  # noqa: E402


class Mapping:
    def __init__(self, document, floorplan):
        path = Path(floorplan)
        self.layout = Layout(path if path.is_dir() else path.parent, document,
                             floorplan=None if path.is_dir() else path)
        self.entries = self.layout.entries
        sources = build_power_sources(document)
        validate_floorplan_entries(self.entries, sources)
        components = thermal_components(document)
        self.component_groups = {d["id"]: np.array([i for i, c in enumerate(components)
                                  if c.get("die", "TOP_DIE") == d["id"]], dtype=int) for d in self.layout.dies}
        self.region_groups = {d["id"]: np.array([self.entries.index(name) for name in d["elements"]], dtype=int)
                              for d in self.layout.dies}
        columns = {c["power_column"]: i for i, c in enumerate(components)}
        self.power_indices = np.array([columns[sources[e]["column"]] if "column" in sources[e] else -1
                                       for e in self.entries])
        self.constants = np.array([sources[e].get("constant_w", 0.) for e in self.entries])
        self.indices, self.weights, self.offsets = [], [], []
        for component in components:
            self.offsets.append(len(self.indices))
            shapes = [geometry_entry(document["geometry"], e)["shape"] for e in component["floorplan_elements"]]
            areas = np.array([float(s[0]) * float(s[1]) for s in shapes])
            self.weights.extend(areas / areas.sum())
            self.indices.extend(self.entries.index(flattened_floorplan_name(e)) for e in component["floorplan_elements"])
        self.indices, self.weights = np.array(self.indices), np.array(self.weights)

    def powers(self, values):
        return np.where(self.power_indices >= 0, values[self.power_indices], self.constants)

    def temperatures(self, values):
        return np.add.reduceat(values[self.indices] * self.weights, self.offsets)


def run(recording, output, server=ROOT / "3D-ICE/bin/3D-ICE-Server", profile="temperature_aware",
        cycles=None, max_seconds=1.,
        single_pass=False, dense_maps=False, timeout=180., factorization_threads=1, thermal_step=None,
        allow_model_clamp=False):
    manifest, document, activity = load_recording(recording)
    paths = manifest["component_paths"]
    layers_per_cycle = manifest.get("layers_per_cycle", 1)
    if profile not in ("constant", "temperature_aware", "recorded"):
        raise ValueError("unknown power profile")
    if (not np.isfinite(max_seconds) or max_seconds <= 0 or round(max_seconds * 1e12) < 1
            or (cycles is not None and cycles < 1) or not np.isfinite(timeout) or timeout <= 0):
        raise ValueError("time cap and cycle count must be positive")
    if manifest["whole_trace"] and not single_pass:
        raise ValueError("whole-program traces can only be replayed with --single-pass")
    if profile == "recorded" and not single_pass:
        raise ValueError("recorded leakage is only valid for single-pass transport validation")
    if manifest["source_profile"] != "constant" and profile != "recorded":
        raise ValueError("temperature feedback replay requires a constant-profile recording")
    if single_pass and cycles is not None:
        raise ValueError("single-pass and cycle count are mutually exclusive")
    if type(factorization_threads) is not int or factorization_threads < 1:
        raise ValueError("factorization thread count must be a positive integer")
    if not Path(server).is_file():
        raise ValueError("3D-ICE server is missing; build it first")
    leakage_model = LeakageModel(document, paths, manifest["temperature_grid_c"])
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    summary = {"status": "running", "recording": str(Path(recording).resolve()), "profile": profile,
               "single_pass": single_pass, "cycles_completed": 0, "slots": 0,
               "layers_per_cycle": layers_per_cycle, "layers_completed": 0,
               "simulated_seconds": 0., "dynamic_energy_j": 0., "leakage_energy_j": 0.,
               "floorplan_constant_energy_j": 0., "peak_component_c": initial_temperature_c(document),
               "time_cap_seconds": max_seconds, "requested_cycles": cycles,
               "allow_model_clamp": allow_model_clamp, "first_model_limit_seconds": None,
               "period_ps": activity.period, "layer_start_ps": activity.start,
               "recording_sha256": manifest["sha256"],
               "server": str(Path(server).resolve()), "server_sha256": digest(server),
               "factorization_threads": factorization_threads}
    write_json(output / "summary.json", summary)
    try:
        step_ps, slot_ps = prepare_stack(recording, output, dense_maps, factorization_threads, thermal_step)
        if slot_ps != manifest["power_interval_ps"]:
            raise ValueError("archived thermal slot differs from the recorded power interval")
        if np.any(np.diff(activity.edges)[:-1] != slot_ps) or np.diff(activity.edges)[-1] > slot_ps:
            raise ValueError("recording has irregular power windows")
        mapping = Mapping(document, output)
        summary.update({"step_ps": step_ps, "slot_ps": slot_ps})
        summary["per_die"] = {die["id"]: {"dynamic_energy_j": 0., "leakage_energy_j": 0.,
                                         "floorplan_constant_energy_j": 0., "total_energy_j": 0.,
                                         "peak_component_c": initial_temperature_c(document)}
                              for die in mapping.layout.dies}
        write_json(output / "components.json", {"paths": paths, "mapping": thermal_components(document)})
        end_ps = round(max_seconds * 1e12)
        requested_end = int(activity.edges[-1]) if single_pass else (
            activity.start + cycles * activity.period if cycles else end_ps)
        end_ps = min(end_ps, requested_end)
        last_progress = time.monotonic()
        energy_dynamic = np.zeros(len(paths))
        energy_leakage = np.zeros(len(paths))
        next_boundary = activity.end
        with ExitStack() as stack:
            histories = {}
            for name in ("dynamic_power_w", "leakage_power_w", "component_temperatures_c"):
                stream = stack.enter_context(gzip.open(output / (name + ".csv.gz"), "wt", newline="", compresslevel=1))
                histories[name] = csv.writer(stream)
                histories[name].writerow(["time_ps", *paths])
            totals = csv.writer(stack.enter_context((output / "timeseries.csv").open("w", newline="")))
            totals.writerow(["time_ps", "dynamic_w", "leakage_w", "constant_w", "peak_component_c"])
            die_totals = None
            if mapping.layout.multidie:
                die_totals = csv.writer(stack.enter_context((output / "die_timeseries.csv").open("w", newline="")))
                die_totals.writerow(["time_ps", "die", "dynamic_w", "leakage_w", "constant_w", "peak_component_c"])
            cycle_file = stack.enter_context((output / "cycles.csv").open("w", newline=""))
            cycle_writer = csv.DictWriter(cycle_file, fieldnames=["cycle", "time_ps", "layers_completed"])
            cycle_writer.writeheader()
            ice = stack.enter_context(IceClient(server, output, timeout))
            if ice.count != len(mapping.entries):
                raise ValueError("server floorplan size differs from the recording")
            initial_state = ice.state()  # Also detect an old server before doing any work.
            temperatures = mapping.temperatures(ice.temperatures(mapping.layout.outputs))
            if np.max(np.abs(initial_state - initial_temperature_c(document))) > 1e-3:
                raise ValueError("stack initial temperature differs from the system contract")
            histories["component_temperatures_c"].writerow([0, *temperatures])
            summary["thermal_cells"] = len(initial_state)
            for index, t0 in enumerate(range(0, end_ps, slot_ps)):
                t1 = t0 + slot_ps
                if single_pass:
                    dynamic = activity.dynamic[index]
                    reference_leakage = activity.leakage[index]
                    energy_duration = int(activity.edges[index + 1] - activity.edges[index]) * 1e-12
                else:
                    dynamic, reference_leakage = activity.average(t0, t1)
                    energy_duration = slot_ps * 1e-12
                leakage = reference_leakage * (leakage_model.factors(temperatures)
                                               if profile == "temperature_aware" else 1.)
                ice.advance(mapping.powers(dynamic + leakage))
                new_temperatures = mapping.temperatures(ice.temperatures(mapping.layout.outputs))
                energy_dynamic += dynamic * energy_duration
                energy_leakage += leakage * energy_duration
                summary.update({"slots": index + 1, "simulated_seconds": t1 * 1e-12,
                                "dynamic_energy_j": float(energy_dynamic.sum()),
                                "leakage_energy_j": float(energy_leakage.sum()),
                                "floorplan_constant_energy_j": summary["floorplan_constant_energy_j"] +
                                                               float(mapping.constants.sum()) * energy_duration,
                                "peak_component_c": max(summary["peak_component_c"], float(new_temperatures.max()))})
                for name, values in (("dynamic_power_w", dynamic), ("leakage_power_w", leakage),
                                     ("component_temperatures_c", new_temperatures)):
                    histories[name].writerow([t1, *[f"{v:.12g}" for v in values]])
                totals.writerow([t1, float(dynamic.sum()), float(leakage.sum()),
                                 float(mapping.constants.sum()), float(new_temperatures.max())])
                for die_id, indices in mapping.component_groups.items():
                    fixed = float(mapping.constants[mapping.region_groups[die_id]].sum())
                    peak = float(new_temperatures[indices].max())
                    metrics = summary["per_die"][die_id]
                    metrics.update(dynamic_energy_j=float(energy_dynamic[indices].sum()),
                                   leakage_energy_j=float(energy_leakage[indices].sum()),
                                   floorplan_constant_energy_j=metrics["floorplan_constant_energy_j"] + fixed * energy_duration,
                                   peak_component_c=max(metrics["peak_component_c"], peak), final_peak_component_c=peak)
                    metrics["total_energy_j"] = sum(metrics[k] for k in
                        ("dynamic_energy_j", "leakage_energy_j", "floorplan_constant_energy_j"))
                    if die_totals is not None:
                        die_totals.writerow([t1, die_id, float(dynamic[indices].sum()), float(leakage[indices].sum()), fixed, peak])
                if not single_pass:
                    while next_boundary <= t1:
                        cycle = summary["cycles_completed"] + 1
                        cycle_writer.writerow({"cycle": cycle, "time_ps": next_boundary,
                                               "layers_completed": cycle * layers_per_cycle})
                        cycle_file.flush()
                        summary["cycles_completed"] = cycle
                        summary["layers_completed"] = cycle * layers_per_cycle
                        summary["last_layer_boundary_ps"] = next_boundary
                        next_boundary += activity.period
                temperatures = new_temperatures
                # Above the characterization range, GVSoC clamps leakage.
                if np.any(temperatures[leakage_model.temperature_dependent] > leakage_model.grid[-1]):
                    if summary["first_model_limit_seconds"] is None:
                        summary["first_model_limit_seconds"] = t1 * 1e-12
                    if not allow_model_clamp:
                        summary["status"] = "model_limit"
                        break
                if time.monotonic() - last_progress >= 10:
                    summary["host_seconds"] = time.monotonic() - started
                    write_json(output / "summary.json", summary)
                    print(f"{index + 1} slots, {t1 * 1e-9:.3f} ms, "
                          f"{summary['cycles_completed']} cycles / {summary['layers_completed']} layers, "
                          f"peak {temperatures.max():.3f} C", flush=True)
                    last_progress = time.monotonic()
            if summary["status"] == "running":
                summary["status"] = ("complete" if requested_end <= round(max_seconds * 1e12)
                                     and (single_pass or cycles is not None) else "time_limit")
            summary["stop_rounding_ps"] = max(0, summary["slots"] * slot_ps - end_ps)
            if not single_pass and summary["cycles_completed"]:
                summary["partial_next_layer_ps"] = summary["slots"] * slot_ps - summary["last_layer_boundary_ps"]
            summary["final_peak_component_c"] = float(temperatures.max())
            np.savez_compressed(output / "final_state.npz", temperatures_c=ice.state(),
                                component_temperatures_c=temperatures,
                                dynamic_energy_j=energy_dynamic, leakage_energy_j=energy_leakage)
    except (KeyboardInterrupt, InterruptedError):
        summary["status"] = "interrupted"
        raise
    except Exception as exc:
        summary.update({"status": "failure", "error": str(exc)})
        raise
    finally:
        summary["host_seconds"] = time.monotonic() - started
        summary["total_energy_j"] = sum(summary[k] for k in
                                        ("dynamic_energy_j", "leakage_energy_j", "floorplan_constant_energy_j"))
        write_json(output / "summary.json", summary)
    return summary
