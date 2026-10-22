"""Create portable recordings from new captures or existing hook traces."""

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

import numpy as np

from .activity import (ROOT, TEMPERATURE_GRID_C, Activity, LeakageModel, digest,
                       load_contract, read_trace, roi_from_log, thermal_components, write_json)
from .layout import Layout, recording_files
from thermal_manifest import copy_inputs


def read_environment(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            words = shlex.split(value)
            if len(words) > 1:
                raise ValueError(f"invalid run.env value for {key}")
            result[key] = words[0] if words else ""
    return result


def recording_stack(path, heat_transfer_coefficient):
    """Override only the copied top heat sink, with SI units at the public API."""
    source = Path(path).read_text()
    if heat_transfer_coefficient is None:
        return source, None
    h = float(heat_transfer_coefficient)
    if not np.isfinite(h) or h <= 0 or h * 1e-12 <= 0:
        raise ValueError("heat-transfer coefficient must be positive and finite in W/(m² K)")
    pattern = r"(\btop\s+heat\s+sink\s*:\s*heat\s+transfer\s+coefficient\s+)([\deE.+-]+)(\s*;)"
    matches = list(re.finditer(pattern, source))
    if len(matches) != 1:
        raise ValueError("missing or ambiguous top heat-sink coefficient in archived stack")
    match = matches[0]
    variant = {"parameter": "top_heat_sink.heat_transfer_coefficient",
               "old_w_per_m2_k": float(match[2]) * 1e12, "new_w_per_m2_k": h,
               "new_w_per_um2_k": h * 1e-12, "source_stack_sha256": digest(path)}
    source = source[:match.start(2)] + f"{h * 1e-12:.17g}" + source[match.end(2):]
    return source, variant


def record(run, output, start=None, end=None, whole_trace=False, layers_per_cycle=1,
           heat_transfer_coefficient=None):
    if type(layers_per_cycle) is not int or layers_per_cycle < 1:
        raise ValueError("layers per cycle must be a positive integer")
    run, output = Path(run).resolve(), Path(output).resolve()
    stack_source, variant = recording_stack(run / "generated/3dice/ice.stk", heat_transfer_coefficient)
    document = load_contract(run / "generated/system_config.json")
    layout = Layout(run / "generated/3dice", document)
    components = thermal_components(document)
    paths = [c["path"] for c in components]
    edges, dynamic, leakage, temperatures = read_trace(run / "traces/power_hook_trace.jsonl", paths)
    if whole_trace:
        if start is not None or end is not None:
            raise ValueError("whole-trace import cannot also specify layer bounds")
        start, end = 0, int(edges[-1])
    elif (start is None) != (end is None):
        raise ValueError("supply both layer bounds")
    elif start is None:
        start, end = roi_from_log(run / "logs/simulator.log")
        workload = run / "artifacts/workload.json"
        if workload.exists() and json.loads(workload.read_text()).get("repeats", 1) != layers_per_cycle:
            raise ValueError("timer ROI repeat count differs from --layers-per-cycle")
    Activity(edges, dynamic, leakage, start, end)
    LeakageModel(document, paths)
    profile = document["metadata"]["power_model_profile"]
    if not whole_trace and profile != "constant":
        raise ValueError("repeated-layer recording requires the constant power profile")
    # Do not overwrite captures/results, even partially completed ones.
    output.mkdir(parents=True, exist_ok=False)
    np.savez_compressed(output / "activity.npz", edges_ps=edges, dynamic_w=dynamic,
                        leakage_w=leakage, input_temperatures_c=temperatures)
    shutil.copyfile(run / "generated/system_config.json", output / "system_config.json")
    if layout.multidie:
        copy_inputs(run / "generated/3dice/thermal_manifest.json", output / "thermal_manifest.json",
                    run / "generated/3dice/ice.stk", output / "ice.stk")
        if variant is not None:
            copied, _ = recording_stack(output / "ice.stk", heat_transfer_coefficient)
            (output / "ice.stk").write_text(copied)
    else:
        (output / "ice.stk").write_text(stack_source)
        shutil.copyfile(layout.dies[0]["floorplan"], output / "floorplan.flp")
    for name in ("workload.json", "manifest.json", "model.json"):
        if (run / "artifacts" / name).exists():
            shutil.copyfile(run / "artifacts" / name, output / ("source_" + name))
    manifest = {"format": f"softhier-thermal-replay-v{document['contract']['version']}", "source_run": str(run),
                "source_trace_sha256": digest(run / "traces/power_hook_trace.jsonl"),
                "source_profile": profile, "whole_trace": whole_trace,
                "layers_per_cycle": layers_per_cycle,
                "component_paths": paths, "layer_start_ps": start, "layer_end_ps": end,
                "period_ps": end - start, "recorded_end_ps": int(edges[-1]),
                "power_interval_ps": int(edges[1]), "temperature_grid_c": TEMPERATURE_GRID_C.tolist(),
                "activity_assumption": "piecewise constant power in each recorded window",
                "sha256": {name: digest(output / name) for name in sorted(recording_files(output, document))}}
    if variant is not None:
        manifest["thermal_variant"] = variant
    write_json(output / "recording.json", manifest)
    return manifest


def _capture(source_run, output, provider, start=None, end=None, layers_per_cycle=1,
             heat_transfer_coefficient=None):
    """Reuse a prebuilt workload; run only GVSoC plus a tiny hook."""
    source_run, output = Path(source_run).resolve(), Path(output).resolve()
    # Reject invalid overrides before starting a potentially expensive capture.
    recording_stack(source_run / "generated/3dice/ice.stk", heat_transfer_coefficient)
    document = load_contract(source_run / "generated/system_config.json")
    Layout(source_run / "generated/3dice", document)
    if document["metadata"]["power_model_profile"] != "constant":
        raise ValueError("capture source must use the constant power profile")
    workload = source_run / "artifacts/workload.json"
    if workload.exists() and json.loads(workload.read_text()).get("repeats", 1) != layers_per_cycle:
        raise ValueError("capture workload repeat count differs from --layers-per-cycle")
    saved = read_environment(source_run / "run.env")
    environment = os.environ.copy()
    # Read data, never source a saved shell file. Do not inherit old output or
    # hook paths: every mutable capture file belongs to this new directory.
    for key in ("SIMULATOR_CONFIG", "SOFTHIER_DIR", "SOFTHIER_SDK_DIR", "SOFTHIER_WORKDIR",
                "SOFTHIER_TARGET", "SOFTHIER_FLOORPLAN", "SOFTHIER_PRELOAD_MODE"):
        if key in saved:
            environment[key] = saved[key]
    if (source_run / "artifacts/arch.py").is_file():
        environment["SIMULATOR_CONFIG"] = str(source_run / "artifacts/arch.py")
    environment["SOFTHIER_POWER_PROFILE"] = "constant"
    for key, artifacts in (
        ("SOFTHIER_BINARY", ("softhier.elf", "layer.elf")),
        ("SIMULATOR_PLATFORM", ("preload.elf",)),
        ("SOFTHIER_INPUT_PRELOAD", ("input.elf",)),
    ):
        path = next((source_run / "artifacts" / name for name in artifacts
                     if (source_run / "artifacts" / name).is_file()), None)
        environment[key] = str(path) if path is not None else saved.get(key, "")
        if key == "SOFTHIER_INPUT_PRELOAD" and not environment[key]:
            continue
        if not Path(environment[key]).is_file():
            raise ValueError(f"missing captured workload artifact: {environment[key]}")
    output.mkdir(parents=True, exist_ok=False)
    for subdir in ("generated/3dice", "traces", "logs", "state", "results/simulator", "artifacts"):
        (output / subdir).mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_run / "generated/3dice", output / "generated/3dice", dirs_exist_ok=True)
    if document["contract"]["version"] == 2:
        copy_inputs(source_run / "generated/3dice/thermal_manifest.json",
                    output / "generated/3dice/thermal_manifest.json",
                    source_run / "generated/3dice/ice.stk", output / "generated/3dice/ice.stk")
        for stem in ("thermal", "dram_power"):
            source = source_run / "artifacts" / f"{stem}_config.json"
            target = output / "artifacts" / f"{stem}_config.json"
            if source.is_file():
                shutil.copyfile(source, target)
            else:
                write_json(target, document["metadata"][f"{stem}_config"])
            environment[f"SOFTHIER_{stem.upper()}_CONFIG"] = str(target)
    for name in ("workload.json", "manifest.json", "model.json"):
        if (source_run / "artifacts" / name).exists():
            shutil.copyfile(source_run / "artifacts" / name, output / "artifacts" / name)
    dramsys = source_run / "artifacts/environment"
    if not dramsys.is_dir():
        dramsys = Path(saved["SOFTHIER_DRAMSYS_PATH"])
    shutil.copytree(dramsys, output / "environment")
    environment["SOFTHIER_DRAMSYS_PATH"] = str(output / "environment")
    environment["SOFTHIER_RUN_CWD"] = str(output / "results/simulator")
    environment["SYSTEM_CONFIG_FILE"] = str(output / "generated/current_system_config.json")
    with (output / "logs/export.log").open("w") as log:
        subprocess.run([str(provider), "export-system"], env=environment, check=True,
                       stdout=log, stderr=subprocess.STDOUT)
    current = load_contract(environment["SYSTEM_CONFIG_FILE"])
    current_metadata = current["metadata"]
    if document["contract"]["version"] == 2:
        archived_thermal = document["metadata"].get("thermal_config", {})
        current_thermal = current_metadata.get("thermal_config", {})
        if "stack_order" not in archived_thermal and current_thermal.get("stack_order") == "dram_on_logic":
            # An omitted selector historically meant DRAM above logic. Compare
            # the new default in that old representation, including its hash;
            # all other metadata and the physical stack must still match.
            legacy_thermal = {key: value for key, value in current_thermal.items() if key != "stack_order"}
            current_metadata = {**current_metadata, "thermal_config": legacy_thermal,
                                "thermal_config_sha256": hashlib.sha256(
                                    json.dumps(legacy_thermal, sort_keys=True).encode()).hexdigest()}
    for key in ("metadata", "geometry", "floorplan", "power_trace", "thermal_feedback", "thermal_stack"):
        value = current_metadata if key == "metadata" else current.get(key)
        if value != document.get(key):
            raise ValueError(f"current simulator {key} differs from archived capture model")
    shutil.copyfile(source_run / "generated/system_config.json", output / "generated/system_config.json")
    updates = []
    for component in thermal_components(document):
        update = {"path": component["path"], "temperature_c": document["thermal_feedback"]["initial_temperature_c"]}
        voltage = component.get("voltage_v")
        if document["contract"]["version"] == 1:
            voltage = document["metadata"]["power_voltage_v"]
        if voltage is not None:
            update["voltage_v"] = voltage
        updates.append(update)
    config = output / "generated/record_hook.json"
    write_json(config, {"temperatures": updates})
    # A wrapper pins the interpreter and works even if the checked-out hook has
    # no executable mode bit. It is created only inside the new capture folder.
    wrapper = output / "record_hook.sh"
    wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " +
                       shlex.quote(str(Path(__file__).with_name("record_hook.py"))) + ' "$@"\n')
    wrapper.chmod(0o755)
    environment.update({"POWER_HOOK_EXECUTABLE": str(wrapper), "POWER_HOOK_CONFIG_FILE": str(config),
                        "POWER_HOOK_REQUEST_FILE": str(output / "state/request.json"),
                        "POWER_HOOK_RESPONSE_FILE": str(output / "state/response.json"),
                        "POWER_HOOK_TRACE_FILE": str(output / "traces/power_hook_trace.jsonl"),
                        "POWER_INTERVAL_PS": saved.get("POWER_INTERVAL_PS", "20000000")})
    write_json(output / "capture.json", {"source_run": str(source_run), "status": "running"})
    started = time.monotonic()
    with (output / "logs/simulator.log").open("w") as log:
        process = subprocess.Popen([str(provider), "run"], env=environment, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            if process.wait() != 0:
                raise RuntimeError(f"GVSoC capture failed; see {output / 'logs/simulator.log'}")
        finally:
            if process.poll() is None:
                import signal
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    manifest = record(output, output / "recording", start, end, layers_per_cycle=layers_per_cycle,
                      heat_transfer_coefficient=heat_transfer_coefficient)
    write_json(output / "capture.json", {"source_run": str(source_run), "status": "complete",
                                        "host_seconds": time.monotonic() - started,
                                        "period_ps": manifest["period_ps"]})
    return manifest


def capture(source_run, output, provider, start=None, end=None, layers_per_cycle=1,
            heat_transfer_coefficient=None):
    if type(layers_per_cycle) is not int or layers_per_cycle < 1:
        raise ValueError("layers per cycle must be a positive integer")
    existed = Path(output).exists()
    started = time.monotonic()
    try:
        return _capture(source_run, output, provider, start, end, layers_per_cycle, heat_transfer_coefficient)
    except BaseException as exc:
        if not existed and Path(output).is_dir():
            write_json(Path(output) / "capture.json", {
                "source_run": str(Path(source_run).resolve()),
                "status": "interrupted" if isinstance(exc, (KeyboardInterrupt, InterruptedError)) else "failure",
                "error": str(exc), "host_seconds": time.monotonic() - started})
        raise
