#!/usr/bin/env python3
"""Validate paired memory-on-logic studies and report logic/DRAM energy and maps."""

import argparse
from collections import defaultdict
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from prepare_workload import ROOT, load_sdk_report, sha256, validate_repeats, within_repo, write_json
from run_study import PROFILES, checked, parse_timing, replay_seconds
from thermal_validation import checked_tmap_statistics, thermal_bounds, validate_temperature


def load_common():
    spec = importlib.util.spec_from_file_location("dram3d_component_analysis",
        ROOT / "experiments/component_power/analyze.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def merge_manifests(paths):
    studies = [json.loads(within_repo(path).read_text()) for path in paths]
    if not studies or any(s.get("experiment") != "llm_dram3d" or s.get("version") != 1 for s in studies):
        raise ValueError("expected version 1 llm_dram3d study manifests")
    for study in studies:
        # Studies created before --repeats always executed the layer once.
        study.setdefault("repeats", 1)
        study.setdefault("opt_step", 0)
        study.setdefault("allow_model_clamp", False)
        validate_repeats(study["repeats"])
        if study.get("thermal_replay") is not None:
            validate_repeats(study["thermal_replay"])
        if study.get("thermal_replay_till") is not None:
            replay_seconds(study["thermal_replay_till"])
            if study.get("thermal_replay") is not None:
                raise ValueError("replay count and duration are mutually exclusive")
        # Older runners used ten thermal integration steps per power interval.
        study.setdefault("thermal_step_ps", study["interval_ps"] / 10)
    first, runs = studies[0], {}
    settings = ("llm_model", "layer", "seed", "all_experts", "repeats", "interval_ps", "thermal_step_ps", "target_cells",
        "estimate_scale", "floorplan_rule", "sdk_commit", "arch_sha256", "source_sha256", "native_source_sha256",
        "thermal_config_sha256", "dram_power_config_sha256", "dram_power_temperature_dependence", "opt_step",
        "thermal_replay", "thermal_replay_till", "allow_model_clamp")
    for study in studies:
        if any(study.get(key) != first.get(key) for key in settings):
            raise ValueError("cannot combine studies with different workload or model settings")
        expected = {(case, profile) for case in study["cases"] for profile in PROFILES}
        actual = {(entry["case"], entry["profile"]) for entry in study["runs"]}
        if not expected or expected != actual or len(actual) != len(study["runs"]):
            raise ValueError("study is incomplete or contains duplicate case/profile runs")
        for entry in study["runs"]:
            key = entry["case"], entry["profile"]
            if key in runs:
                raise ValueError("duplicate case/profile across manifests")
            if entry["interval_ps"] != study["interval_ps"]:
                raise ValueError("run interval differs from study")
            if entry.get("thermal_step_ps", study["thermal_step_ps"]) != study["thermal_step_ps"]:
                raise ValueError("run thermal step differs from study")
            if entry.get("repeats", 1) != study["repeats"]:
                raise ValueError("run repeats differ from study")
            if entry.get("opt_step", 0) != study["opt_step"]:
                raise ValueError("run optimization step differs from study")
            if entry.get("allow_model_clamp", False) != study["allow_model_clamp"]:
                raise ValueError("run leakage clamp setting differs from study")
            if study.get("thermal_replay") is not None and entry.get("replay_cycles") != study["thermal_replay"]:
                raise ValueError("run thermal replay count differs from study")
            if study.get("thermal_replay_till") is not None and entry.get("replay_seconds") != study["thermal_replay_till"]:
                raise ValueError("run thermal replay duration differs from study")
            runs[key] = entry
    merged = dict(first, runs=list(runs.values()), cases=sorted({case for case, _ in runs}),
                  source_manifests=[str(path) for path in paths])
    if first.get("thermal_replay") or first.get("thermal_replay_till"):
        merged["source_runs"] = [entry for study in studies for entry in study.get("source_runs", [])]
    return merged


def die_paths(contract):
    paths = {component["path"]: component["die"]
             for component in contract["thermal_feedback"]["components"]}
    if set(paths.values()) != {"LOGIC_DIE", "DRAM_DIE"}:
        raise ValueError("expected exactly the logic and DRAM active dies")
    return paths


def load_thermal_manifest(run_path):
    folder = within_repo(Path(run_path) / "results/3dice")
    manifest = json.loads((folder / "thermal_manifest.json").read_text())
    if manifest.get("version") != 1 or {die["id"] for die in manifest["dies"]} != {"LOGIC_DIE", "DRAM_DIE"}:
        raise ValueError("expected a complete two-die thermal manifest")
    for die in manifest["dies"]:
        for field in ("floorplan", "temperature_output", "map_output", "coordinates_output"):
            path = within_repo(folder / die[field])
            if not path.is_relative_to(folder) or not path.is_file():
                raise ValueError(f"missing or out-of-run thermal file: {path}")
    return manifest


def check_artifacts(entry, run, study):
    artifacts = run.path / "artifacts"
    for name, expected in (("layer.elf", entry["binary_sha256"]),
                          ("preload.elf", entry["preload_sha256"]),
                          ("arch.py", study["arch_sha256"]),
                          ("thermal_config.json", study["thermal_config_sha256"]),
                          ("dram_power_config.json", study["dram_power_config_sha256"])):
        if sha256(artifacts / name) != expected:
            raise ValueError(f"archived {name} differs from study")
    workload = json.loads((artifacts / "workload.json").read_text())
    if workload.get("repeats", 1) != study["repeats"]:
        raise ValueError("archived workload repeats differ from study")
    if workload.get("opt_step", 0) != study["opt_step"]:
        raise ValueError("archived workload optimization step differs from study")
    for field in ("source_sha256", "native_source_sha256"):
        if workload[field] != study[field]:
            raise ValueError(f"archived {field} differs from study")
    for relative, expected in workload["source_sha256"].items():
        if sha256(artifacts / "source" / relative) != expected:
            raise ValueError("archived SDK source differs from workload")
    for relative, expected in workload["native_source_sha256"].items():
        if sha256(artifacts / "native_source" / relative) != expected:
            raise ValueError("archived native power source differs from workload")
    for relative, expected in workload["dramsys_config_sha256"].items():
        if sha256(artifacts / "environment" / relative) != expected:
            raise ValueError("archived DRAMSys config differs from workload")
    if run.profile != entry["profile"]:
        raise ValueError("run leakage profile differs from study")
    for metadata_key, study_key in (("thermal_config", "resolved_thermal_config"),
                                   ("dram_power_config", "resolved_dram_power_config")):
        if run.metadata.get(metadata_key) != study.get(study_key):
            raise ValueError(f"run {metadata_key} differs from archived input")
    for run_key, study_key in (("floorplan_rule", "floorplan_rule"), ("power_estimate_scale", "estimate_scale")):
        if run.metadata[run_key] != study[study_key]:
            raise ValueError(f"run {run_key} differs from study")
    windows = [record["window"] for record in run.power_records]
    if not windows or any(w["end_ps"] - w["start_ps"] != entry["interval_ps"] for w in windows[:-1]):
        raise ValueError("run interval differs from study")
    if not 0 < windows[-1]["end_ps"] - windows[-1]["start_ps"] <= entry["interval_ps"]:
        raise ValueError("invalid final partial power window")
    numerical = json.loads((run.path / "results/simulator/validation.json").read_text())
    sdk_manifest = json.loads((artifacts / "manifest.json").read_text())
    checked = parse_timing((run.path / "logs/simulator.log").read_text(), sdk_manifest,
        load_sdk_report(artifacts / "source"), run.metadata["cluster_grid"]["count"], study["repeats"],
        require_validation=False, opt_step=study["opt_step"])
    normalized = dict(numerical)
    normalized.setdefault("repeats", 1)
    validate_repeats(normalized["repeats"])
    normalized.setdefault("mean_layer_time_us", normalized["layer_time_us"] / normalized["repeats"])
    if numerical != entry["numerical_validation"] or any(normalized.get(k) != v for k, v in checked.items()):
        raise ValueError("SDK numerical validation differs from log or study")
    return load_thermal_manifest(run.path)


def validate_pair(constant, aware, contracts):
    if constant.profile != "constant" or aware.profile != "temperature_aware":
        raise ValueError("profiles do not form a constant/temperature_aware pair")
    for key in ("geometry", "floorplan", "thermal_feedback", "power_trace", "thermal_stack"):
        if contracts[0].get(key) != contracts[1].get(key):
            raise ValueError(f"A/B {key} differs")
    for key in ("components", "power_model_specs", "power_voltage_v", "power_estimate_scale"):
        if constant.metadata.get(key) != aware.metadata.get(key):
            raise ValueError(f"A/B {key} differs")
    paths = die_paths(contracts[0])
    if len(constant.power_records) != len(aware.power_records):
        raise ValueError("A/B power-window count differs")
    delta = 0.
    for left, right in zip(constant.power_records, aware.power_records):
        if left["window"] != right["window"]:
            raise ValueError("A/B power windows differ")
        a, b = ({item["path"]: item for item in record["components"]} for record in (left, right))
        if set(a) != set(paths) or set(b) != set(paths):
            raise ValueError("power inventory differs from die mappings")
        for path in paths:
            for field in (("dynamic_w", "leakage_w") if paths[path] == "DRAM_DIE" else ("dynamic_w",)):
                if not math.isclose(a[path][field], b[path][field], rel_tol=1e-6, abs_tol=1e-10):
                    raise ValueError(f"A/B {field} differs at {path}")
            delta = max(delta, abs(a[path]["dynamic_w"] - b[path]["dynamic_w"]))
    # DRAM background depends on command/state occupancy even at fixed temperature;
    # only constant-profile LOGIC leakage must be invariant over time.
    reference = {item["path"]: item["leakage_w"] for item in constant.power_records[0]["components"]}
    for record in constant.power_records:
        for item in record["components"]:
            if paths[item["path"]] == "LOGIC_DIE" and not math.isclose(
                    item["leakage_w"], reference[item["path"]], rel_tol=1e-6, abs_tol=1e-10):
                raise ValueError("constant logic leakage changed")
    return delta


def grouped_energy(run, paths):
    info = {item["simulator_path"]: item for item in run.metadata["components"]}
    energy = defaultdict(lambda: dict(dynamic_j=0., leakage_j=0.))
    for record in run.power_records:
        dt = (record["window"]["end_ps"] - record["window"]["start_ps"]) * 1e-12
        for item in record["components"]:
            die = paths[item["path"]]
            kind = "dram" if die == "DRAM_DIE" else info[item["path"]]["kind"]
            for field in ("dynamic", "leakage"):
                energy[die, kind][field + "_j"] += item[field + "_w"] * dt
    return energy


def validate_logic_leakage(common, run, paths):
    # Reuse coefficient verification with a logic-only inventory. The old helper
    # cannot interpret a DRAM power model or its state-dependent background.
    metadata = dict(run.metadata, components=[item for item in run.metadata["components"]
        if paths[item["simulator_path"]] == "LOGIC_DIE"])
    return common.validate_new_logic_leakage(SimpleNamespace(
        metadata=metadata, profile=run.profile, power_records=run.power_records))


def validate_thermal_physics(common, run, contract, manifest):
    folder = run.path / "results/3dice"
    stacks = list(folder.glob("*.stk"))
    if len(stacks) != 1:
        raise ValueError("expected exactly one runtime thermal stack")
    bounds = thermal_bounds(contract, within_repo(stacks[0]).read_text())
    for row in run.temperatures:
        validate_temperature(row["temperature_c"] + 273.15, bounds,
                             row["path"] + " temperature feedback")
    statistics = {}
    for die in manifest["dies"]:
        _, rows = common.VIEW.load_tflp_rows(folder / die["temperature_output"])
        if len(rows) < len(run.power_records):
            raise ValueError(f"missing floorplan thermal slots for {die['id']}")
        for index, row in enumerate(rows):
            for temperature in row["values"]:
                validate_temperature(temperature, bounds, f"{die['id']} floorplan row {index}")
        geometry = common.VIEW.load_tmap_geometry_cells(folder / die["coordinates_output"])
        statistics[die["id"]] = checked_tmap_statistics(folder / die["map_output"],
            geometry["cell_count"], common.np, bounds)
        if statistics[die["id"]][0] < len(run.power_records):
            raise ValueError(f"missing cell thermal slots for {die['id']}")
    return dict(bounds=bounds, cell_statistics=statistics)


def plot_pair(common, case, pair, contracts, output):
    fig, axes = common.plt.subplots(2, 2, figsize=(10, 6.2), layout="constrained")
    for run, contract, color in zip(pair, contracts, ("#0072B2", "#D55E00")):
        paths = die_paths(contract)
        for row, die in enumerate(("LOGIC_DIE", "DRAM_DIE")):
            records = run.power_records
            times = [record["window"]["end_ps"] * 1e-6 for record in records]
            power = [sum(item["total_w"] for item in record["components"]
                         if paths[item["path"]] == die) for record in records]
            axes[row, 0].step([0] + times, [power[0]] + power, where="pre", color=color, label=run.profile)
            temperatures = defaultdict(list)
            for item in run.temperatures:
                if paths[item["path"]] == die:
                    temperatures[item["end_ps"]].append(item["temperature_c"])
            endpoints = sorted(temperatures)
            axes[row, 1].plot([t * 1e-6 for t in endpoints],
                [max(temperatures[t]) for t in endpoints], color=color, label=run.profile)
            axes[row, 0].set(ylabel=f"{die}: power (W)")
            axes[row, 1].set(ylabel=f"{die}: peak component (°C)")
    for axis in axes.flat:
        axis.set_xlabel("Simulation time (µs)")
        axis.ticklabel_format(axis="y", useOffset=False)
    axes[0, 0].legend()
    fig.suptitle(case + ": logic leakage profiles; fixed-temperature DRAM characterization")
    common.BASE.save_figure(fig, output, f"{case}_power_temperature")


def render_maps(common, case, pair, manifests, output, animations, full_maps, statistics=None):
    from matplotlib.collections import PatchCollection
    from matplotlib.patches import Rectangle
    stats = {}
    for die_id in ("LOGIC_DIE", "DRAM_DIE"):
        data, descriptions = [], []
        for pair_index, (run, manifest) in enumerate(zip(pair, manifests)):
            folder = run.path / "results/3dice"
            die = next(item for item in manifest["dies"] if item["id"] == die_id)
            descriptions.append((folder, die))
            data.append(common.VIEW.prepare_gif_data(
                argparse.Namespace(gif_stride=1, vmin=None, vmax=None),
                folder / die["floorplan"], folder / die["temperature_output"], common.np))
            if statistics is None:
                geometry = common.VIEW.load_tmap_geometry_cells(folder / die["coordinates_output"])
                stats[run.profile, die_id] = common.VIEW.read_tmap_gif_stats(
                    folder / die["map_output"], geometry["cell_count"], common.np)
            else:
                stats[run.profile, die_id] = statistics[pair_index][die_id]
            if len(data[-1]["rows"]) < len(run.power_records) or stats[run.profile, die_id][0] < len(run.power_records):
                raise ValueError(f"missing thermal slots for {die_id}")
        minimum, maximum = min(d["data_min"] for d in data), max(d["data_max"] for d in data)
        fig, axes = common.plt.subplots(1, 2, figsize=(9, 4), layout="constrained")
        for axis, run, d in zip(axes, pair, data):
            patches = [Rectangle((r["x"] / 1000, r["y"] / 1000), r["width"] / 1000,
                                  r["height"] / 1000) for r in d["regions"]]
            collection = PatchCollection(patches, cmap="inferno", edgecolors="none")
            collection.set_array(common.np.asarray(d["rows"][-1]["values"]) - 273.15)
            collection.set_clim(minimum - 273.15, maximum - 273.15)
            axis.add_collection(collection)
            axis.autoscale_view()
            axis.set(title=run.profile, xlabel="x (mm)", ylabel="y (mm)", aspect="equal")
            axis.grid(False)
        fig.colorbar(collection, ax=list(axes), label="Component-average temperature (°C)")
        fig.suptitle(f"{case}: {die_id}, final thermal slot")
        common.BASE.save_figure(fig, output, f"{case}_{die_id.lower()}_thermal_maps")
        if animations:
            for run, (folder, die), d in zip(pair, descriptions, data):
                stem = f"{case}_{run.profile}_{die_id.lower()}"
                command = [sys.executable, str(ROOT / "Interface_scripts/plot_runtime_temperature_map/plot_runtime_tmap.py"),
                    "--gif-layout", "map", "--gif-width", "1000", "--gif-writer", "pillow", "--once", "--quiet"]
                subprocess.run(command + ["--floorplan", str(folder / die["floorplan"]),
                    "--tflp", str(folder / die["temperature_output"]), "--gif", str(output / f"{stem}.gif"),
                    "--gif-stride", str(max(1, len(d["rows"]) // 80)), "--vmin", str(minimum), "--vmax", str(maximum),
                    "--slot-seconds", str((run.power_records[0]["window"]["end_ps"] -
                        run.power_records[0]["window"]["start_ps"]) * 1e-12)], check=True)
                if full_maps:
                    values = [stats[r.profile, die_id] for r in pair]
                    subprocess.run(command + ["--coords", str(folder / die["coordinates_output"]),
                        "--map", str(folder / die["map_output"]), "--gif", str(output / f"{stem}_cells.gif"),
                        "--gif-stride", str(max(1, stats[run.profile, die_id][0] // 80)),
                        "--vmin", str(min(v[1] for v in values)), "--vmax", str(max(v[2] for v in values))], check=True)
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--animations", action="store_true")
    parser.add_argument("--full-maps", action="store_true")
    args = parser.parse_args(argv)
    if args.full_maps and not args.animations:
        parser.error("--full-maps requires --animations")
    study = merge_manifests(args.manifest)
    output = within_repo(args.output)
    output.mkdir(parents=True, exist_ok=True)
    os.environ["MPLCONFIGDIR"] = str(output / ".matplotlib")
    if study.get("thermal_replay") or study.get("thermal_replay_till"):
        if args.full_maps:
            parser.error("thermal replay analysis supports --animations for floorplan maps, not --full-maps")
        entries = {(entry["case"], entry["profile"]): entry for entry in study["runs"]}
        for case in study["cases"]:
            command = [sys.executable, "-m", "experiments.thermal_replay.analyze",
                "--constant-run", within_repo(ROOT / entries[case, "constant"]["path"]),
                "--aware-run", within_repo(ROOT / entries[case, "temperature_aware"]["path"]),
                "--output", output / case]
            if args.animations:
                command.append("--animations")
            checked(command, None, output / f"{case}_analysis.log")
        write_json(output / "study.json", study)
        print(f"Wrote thermal replay analysis: {output}", flush=True)
        return
    common = load_common()
    common.BASE.configure_style()
    loaded, entries, contracts, thermal, physical = {}, {}, {}, {}, {}
    for entry in study["runs"]:
        key = entry["case"], entry["profile"]
        run = common.BASE.load_run(within_repo(ROOT / entry["path"]))
        common.validate_inventory(run)
        loaded[key], entries[key] = run, entry
        contracts[key] = json.loads((run.path / "generated/system_config.json").read_text())
        thermal[key] = check_artifacts(entry, run, study)
        physical[key] = validate_thermal_physics(common, run, contracts[key], thermal[key])
    metrics, energies, histories, stages, validation = [], [], [], [], []
    for case in study["cases"]:
        keys = [(case, profile) for profile in PROFILES]
        pair = [loaded[key] for key in keys]
        case_contracts = [contracts[key] for key in keys]
        delta = validate_pair(*pair, case_contracts)
        left, right = (entries[key] for key in keys)
        for field in ("binary_sha256", "preload_sha256"):
            if left[field] != right[field]:
                raise ValueError(f"A/B {field} differs")
        for field in ("stage_cycles", "status", "verified_elements"):
            if left["numerical_validation"][field] != right["numerical_validation"][field]:
                raise ValueError(f"A/B SDK {field} differs")
        plot_pair(common, case, pair, case_contracts, output)
        cell_stats = render_maps(common, case, pair, [thermal[key] for key in keys], output,
                                 args.animations, args.full_maps,
                                 statistics=[physical[key]["cell_statistics"] for key in keys])
        for key, run, contract in zip(keys, pair, case_contracts):
            fields = dict(case=case, profile=run.profile, repeats=study["repeats"])
            paths = die_paths(contract)
            error = validate_logic_leakage(common, run, paths)
            sdk_result = entries[key]["numerical_validation"]
            validation.append(dict(fields, sdk_status=sdk_result["status"],
                verified_elements=sdk_result["verified_elements"],
                numerical_reference_pass=True if sdk_result["status"] == "PASS" else None,
                direct_preload_cycles=0,
                max_dynamic_pair_difference_w=delta, max_logic_leakage_error_w=error,
                physical_thermal_lower_bound_k=physical[key]["bounds"]["lower_bound_k"]))
            energy = grouped_energy(run, paths)
            for (die, kind), values in energy.items():
                energies.append(dict(fields, die=die, component=kind,
                    dynamic_energy_uj=values["dynamic_j"] * 1e6,
                    leakage_or_background_energy_uj=values["leakage_j"] * 1e6))
            temperatures = {(row["end_ps"], row["path"]): row["temperature_c"] for row in run.temperatures}
            for die in ("LOGIC_DIE", "DRAM_DIE"):
                dyn = sum(v["dynamic_j"] for (d, _), v in energy.items() if d == die)
                leak = sum(v["leakage_j"] for (d, _), v in energy.items() if d == die)
                metrics.append(dict(fields, die=die, duration_us=run.duration_ps * 1e-6,
                    layer_time_us=entries[key]["numerical_validation"]["layer_time_us"],
                    mean_layer_time_us=entries[key]["numerical_validation"]["layer_time_us"] / study["repeats"],
                    peak_component_temperature_c=max(row["temperature_c"] for row in run.temperatures if paths[row["path"]] == die),
                    peak_cell_temperature_c=cell_stats[run.profile, die][2] - 273.15,
                    dynamic_energy_uj=dyn * 1e6, leakage_or_background_energy_uj=leak * 1e6,
                    total_energy_uj=(dyn + leak) * 1e6))
                for record in run.power_records:
                    items = [item for item in record["components"] if paths[item["path"]] == die]
                    end = record["window"]["end_ps"]
                    histories.append(dict(fields, die=die, start_us=record["window"]["start_ps"] * 1e-6,
                        end_us=end * 1e-6, dynamic_w=sum(item["dynamic_w"] for item in items),
                        leakage_or_background_w=sum(item["leakage_w"] for item in items),
                        peak_component_temperature_c=max(temperatures[end, item["path"]] for item in items)))
            for stage, cycles in entries[key]["numerical_validation"]["stage_cycles"].items():
                stages.append(dict(fields, stage=stage, cycles=cycles, duration_us=cycles / 1000,
                                   mean_duration_us=cycles / (1000 * study["repeats"])))
    for name, rows in (("metrics", metrics), ("component_energy", energies), ("timeseries", histories),
                       ("stages", stages), ("validation", validation)):
        common.csv_file(output / f"{name}.csv", rows)
    write_json(output / "study.json", study)
    validated_runs = sum(row["sdk_status"] == "PASS" for row in validation)
    if validated_runs == 0:
        numerical_summary = "Final SDK numerical checking was disabled for all runs (`DONE`, `checked=0`)."
    elif validated_runs == len(validation):
        numerical_summary = "Final SDK numerical checking passed for all runs (`PASS`)."
    else:
        numerical_summary = (f"Final SDK numerical checking passed for {validated_runs} of {len(validation)} runs; "
                             "the remaining runs skipped it. See the per-run status in validation.csv.")
    report = ["# Memory-on-logic transformer-layer thermal study", "",
        "One GPT-OSS-120B layer with deterministic synthetic FP16 weights. "
        "The package contains one logic die and one face-to-face DRAM die divided into 16 aligned channel regions. "
        "Controller and PHY power are excluded.", "",
        f"Floorplan: `{study['floorplan_rule']}`; repeats: {study['repeats']}; "
        f"coupling interval: {study['interval_ps'] * 1e-6:g} µs. "
        "Repeats execute the complete layer with the same inputs and KV-cache position. "
        "They extend the thermal history without advancing the token sequence. "
        "Stage counters and layer_time_us accumulate all repeats; mean_layer_time_us is their per-layer mean. "
        "When enabled, numerical checking runs once after the final repeat.", "",
        numerical_summary, "",
        "The constant and temperature-aware profiles change logic leakage only. DRAM command and background "
        "energy use a fixed electrical characterization temperature. The default DRAM electrical profile and "
        "package parameters are engineering estimates, not calibrated device measurements; inspect the archived "
        "resolved_thermal_config and resolved_dram_power_config in study.json for the selected values and provenance.", "",
        "Energy uses exact power-window lengths, including the final partial window. The thermal solver advances "
        "that last window as a full slot. Stage timings exclude untimed preload and SDK numerical checking; "
        "thermal traces include the entire application lifetime.", "",
        "| Case | Logic profile | Die | All repeats (µs) | Mean layer (µs) | Peak component (°C) | Peak cell (°C) | Energy (µJ) |",
        "|---|---|---|---:|---:|---:|---:|---:|"]
    for row in metrics:
        report.append(f"| {row['case']} | {row['profile']} | {row['die']} | {row['layer_time_us']:.3f} | "
            f"{row['mean_layer_time_us']:.3f} | "
            f"{row['peak_component_temperature_c']:.4f} | {row['peak_cell_temperature_c']:.4f} | {row['total_energy_uj']:.3f} |")
    report += ["", "Both profile runs passed the SDK completion and timing checks (11 complete stages). "
        "The analysis verified archived source/input hashes, paired stage "
        "timing, power inventories, interval lengths, logic leakage coefficients, and both die outputs. "
        "DRAM background may vary between intervals with DRAM state occupancy; it must agree across profiles "
        "for the same workload. See validation.csv and each run's archived validation.json.", "",
        "CSV files contain per-die metrics, component-group energy (including the DRAM group), per-die time "
        "histories and SDK stage timing. PNG/PDF figures contain power/temperature traces and separate final "
        "logic and DRAM maps. Optional GIFs use a common color scale within each die/profile pair.", ""]
    (output / "report.md").write_text("\n".join(report))
    print(f"Wrote memory-on-logic analysis: {output}")


if __name__ == "__main__":
    main()
