#!/usr/bin/env python3
"""Validate LLM decode studies and reuse the component-power plots and thermal GIFs."""

import argparse
from collections import defaultdict
import importlib.util
import json
import os
from pathlib import Path
import sys

from prepare_workload import ROOT, sha256, write_json


def load_common():
    path = ROOT / "experiments/component_power/analyze.py"
    spec = importlib.util.spec_from_file_location("llm_component_analysis", path)
    common = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = common
    spec.loader.exec_module(common)
    return common


def merge_manifests(paths):
    studies = [json.loads(Path(path).read_text()) for path in paths]
    first = studies[0]
    if any(s.get("experiment") != "llm_decode" or s.get("version") != 1 for s in studies):
        raise ValueError("expected version 1 llm_decode study manifests")
    settings = ("llm_model", "repeats", "interval_ps", "target_cells", "estimate_scale", "floorplan_rule", "sdk_commit")
    runs = {}
    for study in studies:
        if any(study.get(key) != first.get(key) for key in settings):
            raise ValueError("cannot combine studies with different model, thermal settings, repetitions, or SDK")
        expected = {(batch, profile) for batch in study["decode_batches"]
                    for profile in ("constant", "temperature_aware")}
        actual = {(r["decode_batch"], r["profile"]) for r in study["runs"]}
        if not expected or actual != expected or len(actual) != len(study["runs"]):
            raise ValueError("study is incomplete or contains duplicate batch/profile runs")
        for entry in study["runs"]:
            if entry["llm_model"] != study["llm_model"] or entry["interval_ps"] != study["interval_ps"] or entry["repeats"] != study["repeats"]:
                raise ValueError("run metadata differs from its study")
            key = (entry["decode_batch"], entry["profile"])
            if key in runs:
                raise ValueError("duplicate batch/profile across study manifests")
            runs[key] = entry
    return dict(first, runs=list(runs.values()), decode_batches=sorted({b for b, _ in runs}),
                source_manifests=[str(p) for p in paths])


def check_artifacts(entry, run, manifest):
    for name, field in (("softhier.elf", "binary_sha256"), ("preload.elf", "preload_sha256"),
                        ("input.elf", "input_sha256")):
        if sha256(run.path / "artifacts" / name) != entry[field]:
            raise ValueError(f"archived {name} differs from study manifest: {run.path}")
    if run.profile != entry["profile"]:
        raise ValueError("run leakage profile differs from study manifest")
    for run_key, study_key in (("floorplan_rule", "floorplan_rule"), ("power_estimate_scale", "estimate_scale")):
        if run.metadata[run_key] != manifest[study_key]:
            raise ValueError(f"run {run_key} differs from study manifest")
    windows = [r["window"] for r in run.power_records]
    if not windows or any(w["end_ps"] - w["start_ps"] != entry["interval_ps"] for w in windows[:-1]):
        raise ValueError("run coupling interval differs from study manifest")
    if not 0 < windows[-1]["end_ps"] - windows[-1]["start_ps"] <= entry["interval_ps"]:
        raise ValueError("invalid final partial power window")
    numerical = json.loads((run.path / "results/simulator/validation.json").read_text())
    if not {"output", "routes", "route_weights"}.issubset(numerical) or not all(v["pass_check"] for v in numerical.values()):
        raise ValueError("SDK numerical validation failed or is incomplete")
    if numerical != entry["numerical_validation"]:
        raise ValueError("numerical validation differs from study manifest")
    timing = json.loads((run.path / "results/simulator/timing.json").read_text())
    if timing != entry["timing"] or timing["preload_cycles"] != 0:
        raise ValueError("timing/direct-preload validation differs from study manifest")
    workload = json.loads((run.path / "artifacts/workload.json").read_text())
    for name, expected in workload["dramsys_config_sha256"].items():
        if sha256(run.path / "artifacts/environment" / name) != expected:
            raise ValueError("archived DRAMSys configuration differs from workload manifest")
    return timing


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--animations", action="store_true",
        help="render component-average GIFs for every batch/profile")
    parser.add_argument("--full-maps", action="store_true",
        help="also render thermal-cell GIFs; requires --animations")
    args = parser.parse_args(argv)
    if args.full_maps and not args.animations:
        parser.error("--full-maps requires --animations")
    manifest = merge_manifests(args.manifest)
    args.output.mkdir(parents=True, exist_ok=True)
    # Keep renderer caches inside this experiment unless the caller chose a path.
    os.environ.setdefault("MPLCONFIGDIR", str(args.output.resolve() / ".matplotlib"))
    common = load_common()
    common.BASE.configure_style()
    loaded, entries = defaultdict(dict), {}
    for entry in manifest["runs"]:
        batch, profile = entry["decode_batch"], entry["profile"]
        run = common.BASE.load_run(ROOT / entry["path"])
        check_artifacts(entry, run, manifest)
        loaded[batch][profile], entries[batch, profile] = run, entry
    pairs, rows, energies, histories, validation, stages = {}, [], [], [], [], []
    for batch, profiles in sorted(loaded.items()):
        constant, aware = profiles["constant"], profiles["temperature_aware"]
        key = f"{manifest['llm_model']}_b{batch}"
        common.NAMES[key] = f"{manifest['llm_model']} · batch {batch}"
        pairs[key] = constant, aware
        delta = common.validate_pair(constant, aware)
        left, right = entries[batch, "constant"], entries[batch, "temperature_aware"]
        if left["input_sha256"] != right["input_sha256"] or left["timing"]["stages"] != right["timing"]["stages"]:
            raise ValueError("A/B decoder inputs or stage timing differ")
        if sha256(constant.path / "results/simulator/dump_0") != sha256(aware.path / "results/simulator/dump_0"):
            raise ValueError("A/B numerical outputs differ")
        for run in (constant, aware):
            entry = entries[batch, run.profile]
            fields = dict(llm_model=manifest["llm_model"], decode_batch=batch, profile=run.profile)
            error = common.validate_new_logic_leakage(run)
            validation.append(dict(fields, max_dynamic_pair_difference_w=delta,
                max_logic_leakage_model_error_w=error, direct_preload_cycles=0,
                numerical_reference_pass=True))
            rows.append(dict(fields, layer_ms=entry["timing"]["layer_ms"],
                layer_cycles=entry["timing"]["layer_cycles"], **common.metrics(run)))
            for stage, cycles in entry["timing"]["stages"].items():
                stages.append(dict(fields, stage=stage, cycles=cycles, duration_us=cycles / 1000))
            clocks = common.logic_clock_energy(run)
            for kind, energy in common.grouped_energy(run).items():
                if kind in clocks and energy["dynamic_j"] < clocks[kind] - 1e-12:
                    raise ValueError(f"dynamic energy below residual clock budget: {kind}")
                energies.append(dict(fields, component=kind,
                    dynamic_energy_uj=energy["dynamic_j"] * 1e6,
                    leakage_energy_uj=energy["leakage_j"] * 1e6,
                    new_logic_clock_energy_uj=clocks.get(kind, 0) * 1e6))
            info = common.inventory(run)
            temperatures = {(r["end_ps"], r["path"]): r["temperature_c"] for r in run.temperatures}
            for record in run.power_records:
                start, end = record["window"]["start_ps"], record["window"]["end_ps"]
                for kind in common.KINDS:
                    components = [c for c in record["components"] if info[c["path"]]["kind"] == kind]
                    if components:
                        histories.append(dict(fields, component=kind, start_us=start * 1e-6,
                            end_us=end * 1e-6, dynamic_w=sum(c["dynamic_w"] for c in components),
                            leakage_w=sum(c["leakage_w"] for c in components),
                            peak_temperature_c=max(temperatures[end, c["path"]] for c in components)))
        common.plot_pair(key, constant, aware, args.output)
        common.render_maps(key, constant, aware, args.output, args.animations, args.full_maps)
    common.plot_summary(pairs, args.output)
    common.plot_floorplan(next(iter(pairs.values()))[1], args.output)
    common.plot_assumptions(args.output)
    for name, data in (("metrics", rows), ("component_energy", energies), ("timeseries", histories),
                       ("validation", validation), ("stages", stages)):
        common.csv_file(args.output / f"{name}.csv", data)
    write_json(args.output / "study.json", manifest)
    report = [f"# {manifest['llm_model']} decoder-layer thermal co-simulation", "",
        "This study runs the SDK's complete full-attention decoder layer with synthetic FP16 "
        "weights and a 2,048-token cache. It is one layer, not an end-to-end 120B model. "
        "Each batch uses the same executable and weights across both leakage profiles.", "",
        f"Floorplan: `{manifest['floorplan_rule']}`; repeats: {manifest['repeats']}; "
        f"coupling interval: {manifest['interval_ps'] / 1e6:g} µs; "
        f"thermal mesh target: {manifest['target_cells']} cells.", "",
        "ELF preload finishes at cycle zero. Layer time comes from the SDK's 14 stage counters; "
        "thermal duration includes program initialization and output reporting. Energy integrates "
        "the actual power windows, including the final partial window. 3D-ICE advances that final "
        "window as one full thermal slot.", "",
        "| Batch | Leakage profile | Layer (ms) | Thermal run (ms) | Peak component (°C) | Peak cell (°C) | Total energy (µJ) |",
        "|---:|---|---:|---:|---:|---:|---:|"]
    for row in rows:
        report.append(f"| {row['decode_batch']} | {row['profile']} | {row['layer_ms']:.6f} | "
            f"{row['duration_us'] / 1000:.6f} | {row['peak_component_temperature_c']:.4f} | "
            f"{row['peak_cell_temperature_c']:.4f} | {row['total_energy_uj']:.3f} |")
    report += ["", "## Validation", "",
        "Both profiles passed the SDK's independent NumPy reference (exact routing IDs; "
        "FP16 output tolerance 0.01 absolute, 0.02 relative). Their input/executable/weight hashes, "
        "stage counters, numerical dumps, thermal mappings, and per-window dynamic power match. "
        "New-logic leakage is checked against the archived model tables and the temperature used. "
        "See `validation.csv`, `stages.csv`, and each run's `results/simulator/validation.json`.", "",
        "## Figures and data", "",
        "Static figures are PNG/PDF; `metrics.csv`, `component_energy.csv`, `timeseries.csv`, "
        "and `stages.csv` contain the plotted data. GIFs share a color scale within each batch pair. "
        "The `_cells.gif` versions resolve thermal-cell gradients; the other GIFs show component averages.", "",
        "![Power and energy](workload_summary.png)", "",
        "![Cluster floorplan](cluster_floorplan.png)", ""]
    if args.animations:
        report += ["| Batch | Leakage profile | Component averages | Thermal cells |",
                   "|---:|---|---|---|"]
        for batch in sorted(loaded):
            for profile in ("constant", "temperature_aware"):
                stem = f"{manifest['llm_model']}_b{batch}_{profile}"
                cells = f"[GIF]({stem}_cells.gif)" if args.full_maps else "—"
                report.append(f"| {batch} | {profile} | [GIF]({stem}.gif) | {cells} |")
        report.append("")
    report += ["## Interpretation", "",
        "The workload uses full model dimensions but synthetic weights and routing. Repeats reuse "
        "the same input and cache position, rather than generating subsequent tokens. Power and "
        "areas are estimates; HBM/PHY power is outside the thermal boundary. Initial temperature "
        "is 26.85 °C while fixed leakage is referenced to 25 °C. These runs measure transient "
        "cold-start heating, not thermal steady state.", ""]
    (args.output / "report.md").write_text("\n".join(report))
    print(f"Wrote LLM decode analysis: {args.output}")


if __name__ == "__main__":
    main()
