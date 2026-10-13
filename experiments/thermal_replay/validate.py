"""Quantitative checks against archived full co-simulations; no silent equivalence claims."""

import json
from pathlib import Path

import numpy as np

from .activity import LeakageModel, load_contract, load_recording, read_trace, thermal_components, write_json


def validate_leakage(constant_run, aware_run):
    constant_run, aware_run = Path(constant_run), Path(aware_run)
    document = load_contract(constant_run / "generated/system_config.json")
    aware_document = load_contract(aware_run / "generated/system_config.json")
    for field in ("geometry", "floorplan", "thermal_feedback"):
        if document[field] != aware_document[field]:
            raise ValueError(f"paired runs differ in {field}")
    for field in ("power_model_specs", "technology_node", "power_voltage_v", "power_frequency_hz", "components"):
        if document["metadata"][field] != aware_document["metadata"][field]:
            raise ValueError(f"paired runs differ in {field}")
    if (document["metadata"]["power_model_profile"] != "constant" or
            aware_document["metadata"]["power_model_profile"] != "temperature_aware"):
        raise ValueError("expected a constant/temperature_aware pair")
    paths = [c["path"] for c in thermal_components(document)]
    edges, dynamic, reference, _ = read_trace(constant_run / "traces/power_hook_trace.jsonl", paths)
    aware_edges, aware_dynamic, observed, temperatures = read_trace(aware_run / "traces/power_hook_trace.jsonl", paths)
    if not np.array_equal(edges, aware_edges):
        raise ValueError("paired runs have different event timing")
    model = LeakageModel(document, paths)
    predicted = np.array([base * model.factors(temp) for base, temp in zip(reference, temperatures)])
    error = np.abs(predicted - observed)
    kinds = {c["simulator_path"]: c["kind"] for c in document["metadata"]["components"]}
    dynamic_error = float(np.max(np.abs(dynamic - aware_dynamic)))
    leakage_error = float(error.max())
    return {"dynamic_max_error_w": dynamic_error, "leakage_max_error_w": leakage_error,
            "leakage_max_error_w_by_kind": {kind: float(error[:, [kinds[p] == kind for p in paths]].max())
                                            for kind in sorted(set(kinds.values()))},
            "passed": dynamic_error <= 1e-10 and leakage_error <= 1e-9,
            "windows": len(dynamic), "components": len(paths)}


def compare_replay(replay_output, reference_run, single_pass=False):
    replay_output, reference_run = Path(replay_output), Path(reference_run)
    summary = json.loads((replay_output / "summary.json").read_text())
    document = load_contract(reference_run / "generated/system_config.json")
    recording, replay_document, _ = load_recording(summary["recording"])
    for field in ("geometry", "floorplan", "thermal_feedback"):
        if replay_document[field] != document[field]:
            raise ValueError(f"replay and reference differ in {field}")
    for field in ("power_model_specs", "technology_node", "power_voltage_v", "power_frequency_hz", "components"):
        if replay_document["metadata"][field] != document["metadata"][field]:
            raise ValueError(f"replay and reference differ in {field}")
    profile = recording["source_profile"] if summary["profile"] == "recorded" else summary["profile"]
    if profile != document["metadata"]["power_model_profile"]:
        raise ValueError("replay and reference use different power profiles")
    # Ignore only output destinations and floorplan filenames; all thermal
    # physics and discretization must agree for an equivalence comparison.
    import re
    def physics(path):
        text = re.split(r"\boutput\s*:", path.read_text())[0]
        text = re.sub(r'\bfloorplan\s+"[^"]+"', 'floorplan "recording.flp"', text)
        text = re.sub(r"\bnumofcores\s+\d+\s*;", "", text)
        # Layer type/instance names changed between the archived studies, while
        # their material, dimensions and stack order remained identical.
        names = re.findall(r"\blayer\s+(\w+)\s*:", text)
        substitutions = {name: f"REPLAY_LAYER_{i}" for i, name in enumerate(names)}
        text = re.sub(r"\b\w+\b", lambda m: substitutions.get(m[0], m[0]), text)
        before, stack = re.split(r"\bstack\s*:", text, maxsplit=1)
        stack, solver = re.split(r"\bsolver\s*:", stack, maxsplit=1)
        stack = re.sub(r"\b(layer|die)\s+\w+\s+(\w+)", r"\1 INSTANCE \2", stack)
        text = before + "stack:" + stack + "solver:" + solver
        return "".join(text.split())
    if physics(Path(summary["recording"]) / "ice.stk") != physics(reference_run / "generated/3dice/ice.stk"):
        raise ValueError("replay and reference use different thermal stacks")
    if physics(replay_output / "ice.stk") != physics(reference_run / "generated/3dice/ice.stk"):
        raise ValueError("replay runtime and reference use different thermal stacks or integration steps; "
                         "use --thermal-step to match the reference step")
    paths = [c["path"] for c in thermal_components(document)]
    replay_components = json.loads((replay_output / "components.json").read_text())
    if paths != replay_components["paths"]:
        raise ValueError("reference and replay component order differs")
    edges, dynamic, leakage, _ = read_trace(reference_run / "traces/power_hook_trace.jsonl", paths)
    reference_energy = float(np.sum((dynamic + leakage) * np.diff(edges)[:, None] * 1e-12))
    replay_energy = summary["dynamic_energy_j"] + summary["leakage_energy_j"]
    energy_error = abs(replay_energy / reference_energy - 1)
    reference_temps = np.loadtxt(reference_run / "results/3dice/output_top_die_flp_avg.txt", comments="%", ndmin=2)
    actual_temps = np.loadtxt(replay_output / "floorplan_temperatures.txt", comments="%", ndmin=2)
    if actual_temps.shape[1] != reference_temps.shape[1]:
        raise ValueError("reference and replay floorplan output widths differ")
    # Check names too: width alone cannot detect a permuted floorplan.
    def header(path):
        with path.open() as stream:
            for line in stream:
                if line.startswith("%") and "Time(s)" in line:
                    return line.split()[2:]
        raise ValueError(f"missing Tflp header: {path}")
    if header(reference_run / "results/3dice/output_top_die_flp_avg.txt") != header(replay_output / "floorplan_temperatures.txt"):
        raise ValueError("reference and replay floorplan order differs")
    peak_error = float(abs(actual_temps[:, 1:].max() - reference_temps[:, 1:].max()))
    report = {"replay_output": str(replay_output.resolve()), "reference_run": str(reference_run.resolve()),
              "reference_energy_j": reference_energy, "replay_energy_j": replay_energy,
              "relative_energy_error": energy_error, "peak_temperature_error_c": peak_error,
              "reference_duration_seconds": float(edges[-1]) * 1e-12,
              "replay_duration_seconds": summary["simulated_seconds"],
              "replay_host_seconds": summary["host_seconds"], "status": summary["status"],
              "energy_tolerance": .01, "peak_temperature_tolerance_c": .1,
              "passed": summary["status"] == "complete" and energy_error <= .01 and peak_error <= .1}
    if single_pass:
        same_shape = actual_temps.shape == reference_temps.shape
        same_times = same_shape and np.allclose(actual_temps[:, 0], reference_temps[:, 0], rtol=0, atol=1e-10)
        report["all_temperature_rows_max_error_c"] = (
            float(abs(actual_temps[:, 1:] - reference_temps[:, 1:]).max()) if same_times else None)
        report["passed"] &= same_times and report["all_temperature_rows_max_error_c"] <= .1
    timing = reference_run / "results/simulator/timing.json"
    if timing.exists():
        original_seconds = json.loads(timing.read_text()).get("host_seconds")
        if original_seconds:
            report["reference_host_seconds"] = original_seconds
            report["measured_speedup"] = original_seconds / summary["host_seconds"]
    return report


def validate(output, constant_run=None, aware_run=None, replay_output=None, reference_run=None, single_pass=False):
    report = {}
    if constant_run is not None and aware_run is not None:
        report["leakage"] = validate_leakage(constant_run, aware_run)
    elif constant_run is not None or aware_run is not None:
        raise ValueError("supply both --constant-run and --aware-run")
    if replay_output is not None and reference_run is not None:
        report["replay"] = compare_replay(replay_output, reference_run, single_pass)
    elif replay_output is not None or reference_run is not None:
        raise ValueError("supply both --replay-output and --reference-run")
    if not report:
        raise ValueError("supply a leakage pair or replay/reference pair to validate")
    report["passed"] = all(value["passed"] for value in report.values())
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    write_json(output, report)
    return report
