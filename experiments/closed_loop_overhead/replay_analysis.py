"""Exclusive capture/replay wall time; no estimated architecture costs."""

import csv
import json
import math
from pathlib import Path
import random
import statistics

from .analyze import partition, percentile, power_accounting_counter, read_events
from . import backend
from experiments.thermal_replay.activity import digest, load_recording, write_json

PHASES = ("architecture", "power_estimation", "thermal_advancement", "coupling_output")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def markers(events, prefix, start, end):
    first = [e for e in events if e["phase"] == prefix + "_start"]
    last = [e for e in events if e["phase"] == prefix + "_end"]
    require(len(first) == len(last) == 1, f"expected one {prefix} start/end pair")
    a, b = first[0], last[0]
    require(start <= a["start_ns"] < b["end_ns"] <= end and a["pid"] == b["pid"],
            f"invalid {prefix} host boundaries")
    require(b["sim_ps"] > a["sim_ps"], f"invalid {prefix} simulated duration")
    return a, b


def four_phases(values):
    return dict(architecture=values["architecture"],
                power_estimation=values["inline_power_accounting"] + values["power_capture"],
                thermal_advancement=values["thermal_advancement"],
                coupling_output=values["coupling_output"])


def summarize_capture(events, start, end, interval_ps):
    a, b = markers(events, "roi", start, end)
    roi = a["start_ns"], b["end_ns"]
    counter = power_accounting_counter(events, roi, required=True)
    require(counter["pid"] == a["pid"] and counter["window_start_ps"] == a["sim_ps"]
            and counter["window_end_ps"] == b["sim_ps"], "capture power counter differs from ROI")
    require(not any(e["phase"] in ("thermal_step", "factorization", "preconditioner_setup", "mesh_cells") for e in events),
            "capture must not run a thermal solver")
    exchanges = [e for e in events if e["phase"] == "exchange" and e["value"] > 0]
    require(exchanges and all(e["value"] <= interval_ps for e in exchanges),
            "capture requires power exchanges at the requested interval")
    refined = [dict(e, phase="power_capture") if e["phase"] == "power_mapping" else e for e in events]
    phases = partition(refined, *roi, roi)
    return dict(process_seconds=(end - start) * 1e-9, roi_seconds=(roi[1] - roi[0]) * 1e-9,
                start_ns=start, end_ns=end, roi_start_ns=roi[0], roi_end_ns=roi[1],
                roi_start_ps=a["sim_ps"], roi_end_ps=b["sim_ps"],
                roi_simulated_ps=b["sim_ps"] - a["sim_ps"],
                initialization_seconds=(roi[0] - start) * 1e-9,
                finalization_seconds=(end - roi[1]) * 1e-9,
                phases_seconds=four_phases(phases), power_accounting=counter,
                power_capture_mapping_seconds=phases["power_capture"], exchange_count=len(exchanges),
                thermal_steps=0)


def summarize_replay(events, start, end, summary, settings):
    interval = settings["interval_ps"]
    duration = settings["replay_simulated_ps"]
    count = duration // interval
    require(summary["status"] in ("complete", "time_limit"), "replay did not reach its requested duration")
    require(summary["profile"] == "temperature_aware" and not summary["single_pass"]
            and summary["requested_cycles"] is None, "expected repeated temperature-aware replay")
    require(summary["slot_ps"] == summary["step_ps"] == interval and duration % interval == 0,
            "thermal step and power slot must match")
    require(summary["thermal_cells"] == settings["native_cells"], "replay thermal mesh differs")
    require(summary["factorization_threads"] == settings["thermal_threads"], "replay thermal threads differ")
    selected = backend.from_settings(settings)
    if "thermal_backend" in settings:
        backend.verify_metadata(summary, selected)
    else:
        require(summary.get("solver_type", backend.LU) == backend.LU, "legacy study requires LU")
    require(summary["slots"] == count and round(summary["simulated_seconds"] * 1e12) == duration
            and summary["stop_rounding_ps"] == 0, "replay is incomplete or rounded beyond the requested duration")
    a, b = markers(events, "replay", start, end)
    require(a["sim_ps"] == 0 and b["sim_ps"] == duration, "replay markers differ from its duration")
    roi = a["start_ns"], b["end_ns"]
    steps = sorted((e for e in events if e["phase"] == "thermal_step"), key=lambda e: e["sim_ps"])
    powers = sorted((e for e in events if e["phase"] == "replay_power"), key=lambda e: e["sim_ps"])
    require(len(steps) == len(powers) == count, "missing native steps or replay power spans")
    previous_end = roi[0]
    for i, (step, power) in enumerate(zip(steps, powers)):
        require(abs(step["sim_ps"] - i * interval) <= 1 and power["sim_ps"] == i * interval,
                "duplicate or missing replay slot")
        require(previous_end <= power["start_ns"] <= power["end_ns"] <= step["start_ns"]
                < step["end_ns"] <= roi[1], "power calculation and native solver boundaries disagree")
        previous_end = step["end_ns"]
    setups = backend.setup_events(events, selected["solver_type"], start, roi[0])
    factors = [e for e in setups if e["phase"] == "factorization"]
    cells = [e["value"] for e in events if e["phase"] == "mesh_cells"]
    require(cells == [settings["native_cells"]], "expected matching native cell count")
    require(not any(e["phase"] == "inline_power_accounting" for e in events),
            "replay must not include SoftHier accounting counters")
    # The whole loop defaults to coordination/output. Native solver intervals
    # override blocked client waits; explicit power spans override the remainder.
    refined = [dict(e, phase="power_capture") if e["phase"] == "replay_power" else e for e in events]
    refined.append(dict(phase="exchange", start_ns=roi[0], end_ns=roi[1]))
    phases = four_phases(partition(refined, *roi, roi))
    require(phases["architecture"] == 0, "replay unexpectedly contains architecture execution")
    result = dict(process_seconds=(end - start) * 1e-9, loop_seconds=(roi[1] - roi[0]) * 1e-9,
                start_ns=start, end_ns=end, loop_start_ns=roi[0], loop_end_ns=roi[1],
                initialization_seconds=(roi[0] - start) * 1e-9,
                finalization_seconds=(end - roi[1]) * 1e-9, simulated_ps=duration,
                phases_seconds=phases, thermal_steps=count, factorizations=len(factors), native_cells=cells[0],
                detail_seconds={phase: sum((e["end_ns"] - e["start_ns"]) * 1e-9
                                           for e in events if e["phase"] == phase)
                                for phase in ("matrix_preparation", "factorization", "thermal_sources",
                                              "thermal_rhs", "thermal_solve", "thermal_output")})
    if "thermal_backend" in settings:
        result["preconditioner_setups"] = len(setups) - len(factors)
        result["detail_seconds"]["preconditioner_setup"] = sum(
            (e["end_ns"] - e["start_ns"]) * 1e-9 for e in setups if e["phase"] == "preconditioner_setup")
    # Keep the exact legacy result shape for validation of saved measurements.
    return result


def combine(capture, replay, workflow_start_ns, *, recovered_pilot=False):
    require(workflow_start_ns <= capture["start_ns"] < capture["end_ns"] <= replay["start_ns"]
            < replay["end_ns"], "capture and replay must run sequentially")
    phases = {key: capture["phases_seconds"][key] + replay["phases_seconds"][key] for key in PHASES}
    runtime = capture["roi_seconds"] + replay["loop_seconds"]
    excluded = dict(capture_setup=(capture["start_ns"] - workflow_start_ns) * 1e-9,
                    capture_initialization=capture["initialization_seconds"],
                    capture_finalization=capture["finalization_seconds"],
                    recording_preparation=(replay["start_ns"] - capture["end_ns"]) * 1e-9,
                    replay_initialization=replay["initialization_seconds"],
                    replay_finalization=replay["finalization_seconds"])
    if recovered_pilot:
        # A recovered pilot is never a performance sample. Keep its elapsed
        # recovery gap visible instead of calling human idle time conversion.
        excluded["pilot_recovery_gap"] = excluded.pop("recording_preparation")
    total = (replay["end_ns"] - workflow_start_ns) * 1e-9
    require(all(math.isfinite(v) and v >= 0 for v in [*phases.values(), *excluded.values()])
            and math.isclose(sum(phases.values()), runtime, abs_tol=1e-8)
            and math.isclose(runtime + sum(excluded.values()), total, abs_tol=1e-8),
            "combined timing does not reconcile")
    seconds = replay["simulated_ps"] * 1e-12
    return dict(scope="capture_roi_plus_replay_loop", capture_count=1, runtime_seconds=runtime,
                phases_seconds=phases, excluded_seconds=excluded, end_to_end_seconds=total,
                host_seconds_per_simulated_second_with_capture=runtime / seconds,
                host_seconds_per_simulated_second_replay_only=replay["loop_seconds"] / seconds,
                end_to_end_host_seconds_per_simulated_second=total / seconds,
                power_components_seconds=dict(inline_power_accounting=capture["power_accounting"]["seconds"],
                    power_capture_mapping=capture["power_capture_mapping_seconds"],
                    replay_power_accounting=replay["phases_seconds"]["power_estimation"]))


def validate_capture(directory, row, manifest):
    """Validate a completed capture independently of replay startup."""
    for relative, checksum in row.get("capture_artifact_sha256", {}).items():
        require(digest(directory / relative) == checksum, f"capture artifact changed: {relative}")
    source = row["capture"]
    capture = summarize_capture(read_events(directory / "capture/timing"), source["start_ns"],
                                source["end_ns"], manifest["settings"]["interval_ps"])
    validation = row["validation"]
    signature = manifest["architectural_signature"]
    require(validation["status"] == "PASS" and validation["cycles"] == signature["cycles"]
            and validation["stage_cycles"] == signature["stage_cycles"]
            and capture["roi_simulated_ps"] == signature["roi_simulated_ps"],
            "capture failed numerical or architectural validation")
    recording, _, _ = load_recording(directory / "recording")
    require(recording["source_profile"] == "constant" and not recording["whole_trace"]
            and recording["layers_per_cycle"] == 1
            and recording["power_interval_ps"] == manifest["settings"]["interval_ps"]
            and recording["layer_start_ps"] == capture["roi_start_ps"]
            and recording["layer_end_ps"] == capture["roi_end_ps"]
            and recording["source_trace_sha256"] == digest(directory / "capture/traces/power_hook_trace.jsonl"),
            "recording differs from measured capture")
    config = json.loads((directory / "capture/results/simulator/gvsoc_config.json").read_text())
    require(config["target"]["gvsoc"]["power"] is True
            and row["effective_capture_configuration"]["dram_power_analysis"] is True,
            "capture power accounting was disabled")
    require(capture == row["capture"], "stored capture timing differs from raw evidence")
    return capture, recording


def validate_measurement(directory, row, manifest):
    """Reconcile saved measurements against raw evidence before reporting."""
    require(row["status"] == "complete", "workflow is not complete")
    for relative, checksum in row["artifact_sha256"].items():
        require(digest(directory / relative) == checksum, f"workflow artifact changed: {relative}")
    require(not row.get("reuse_capture") or row["pilot"], "measured workflows must use fresh captures")
    capture, recording = validate_capture(directory, row, manifest)
    summary = json.loads((directory / "replay/summary.json").read_text())
    require(summary["recording_sha256"] == recording["sha256"], "replay used a different recording")
    require(summary["server_sha256"] == manifest["reference"]["input_sha256"].get(summary["server"]),
            "replay native server differs from reference")
    source = row["replay"]
    replay = summarize_replay(read_events(directory / "replay_timing"), source["start_ns"],
                              source["end_ns"], summary, manifest["settings"])
    combined = combine(capture, replay, row["workflow_start_ns"], recovered_pilot=bool(row.get("reuse_capture")))
    require(capture == row["capture"] and replay == row["replay"] and combined == row["combined"],
            "stored timing differs from raw capture/replay evidence")
    return combined


def load_results(study, require_complete=False):
    study = Path(study).resolve()
    manifest = json.loads((study / "study.json").read_text())
    require(manifest.get("experiment") == "capture_replay_overhead_v1", "not a capture/replay overhead study")
    rows, excluded, hashes = [], [], {str(study / "study.json"): digest(study / "study.json")}
    for item in manifest["schedule"]:
        p = study / "runs" / item["id"] / "measurement.json"
        if item["pilot"]:
            excluded.append(dict(id=item["id"], reason="pilot"))
            continue
        row = json.loads(p.read_text()) if p.exists() else None
        if row is None or row["status"] != "complete":
            excluded.append(dict(id=item["id"], reason=row["status"] if row else "missing"))
            continue
        require(all(row[k] == item[k] for k in ("id", "block", "pilot")), "workflow schedule changed")
        validate_measurement(p.parent, row, manifest)
        hashes[str(p)] = digest(p)
        hashes.update({str(p.parent / key): value for key, value in row["artifact_sha256"].items()})
        rows.append(row)
    require(rows, "no complete measured capture/replay workflow")
    if require_complete:
        require(manifest["status"] == "complete" and len(rows) == manifest["settings"]["replicates"],
                "the complete capture/replay study is required for the figure")
    means = lambda values: statistics.mean(values)
    combined = {key: means(r["combined"][key] for r in rows) for key in
                ("runtime_seconds", "end_to_end_seconds", "host_seconds_per_simulated_second_with_capture",
                 "host_seconds_per_simulated_second_replay_only", "end_to_end_host_seconds_per_simulated_second")}
    combined.update({field: {key: means(r["combined"][field][key] for r in rows)
                             for key in rows[0]["combined"][field]}
                     for field in ("phases_seconds", "excluded_seconds", "power_components_seconds")})
    ci = None
    if len(rows) > 1:
        rng = random.Random(manifest["settings"]["seed"])
        values = [r["combined"]["runtime_seconds"] for r in rows]
        samples = [means(rng.choices(values, k=len(values))) for _ in range(10000)]
        ci = [percentile(samples, .025), percentile(samples, .975)]
    return dict(schema_version=1, study=str(study), status=manifest["status"], settings=manifest["settings"],
                reference=manifest["reference"], architectural_signature=manifest["architectural_signature"],
                complete_blocks=len(rows), included_runs=[r["id"] for r in rows], excluded_runs=excluded,
                input_sha256=hashes, combined=combined, runtime_ci95_seconds=ci,
                stage_phases_seconds={stage: {phase: means(r[stage]["phases_seconds"][phase] for r in rows)
                                             for phase in PHASES} for stage in ("capture", "replay")},
                statistics="Pilot excluded; one capture per workflow; independent workflow bootstrap only when n >= 2",
                measurement_scope="capture_roi_plus_replay_loop", rows=rows)


def analyze(study):
    result = load_results(study)
    directory = Path(study)
    write_json(directory / "analysis.json", {k: v for k, v in result.items() if k != "rows"})
    flat = []
    for row in result["rows"]:
        data = dict(id=row["id"], capture_count=1, replay_simulated_seconds=row["replay"]["simulated_ps"] * 1e-12)
        data.update({k: v for k, v in row["combined"].items() if isinstance(v, (int, float, str))})
        for stage in ("capture", "replay", "combined"):
            data.update({f"{stage}_{k}_seconds": v for k, v in row[stage]["phases_seconds"].items()})
        data.update({f"excluded_{k}_seconds": v for k, v in row["combined"]["excluded_seconds"].items()})
        flat.append(data)
    with (directory / "runs.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(flat[0]))
        writer.writeheader(); writer.writerows(flat)
    with (directory / "summary.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["stage", "phase", "mean_seconds", "measured_workflows"])
        for stage, values in {**result["stage_phases_seconds"], "combined": result["combined"]["phases_seconds"],
                              "excluded": result["combined"]["excluded_seconds"]}.items():
            writer.writerows([stage, key, value, result["complete_blocks"]] for key, value in values.items())
    return result
