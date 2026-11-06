"""Short real capture/replay validation on the reference 67,139-cell mesh."""

import gzip
import json
from pathlib import Path
import re
import time

import numpy as np

from . import runner, backend
from .analyze import read_events
from .replay_analysis import combine, require, summarize_capture, summarize_replay
from .replay_overhead import capture_setup, load_reference, replay_command
from .smoke import prepare_smoke
from experiments.thermal_replay.activity import digest, load_recording, read_trace, write_json
from experiments.thermal_replay.record import record


def run(output, reference_study):
    output = Path(output).resolve()
    reference = load_reference(reference_study)
    output.mkdir(parents=True, exist_ok=False)
    _, binary, _ = prepare_smoke(output / "kernel", reference["workload"]["arch"])
    require(digest(output / "kernel/arch.py") == digest(reference["workload"]["arch"]),
            "smoke kernel and reference architecture differ")
    cpus = reference["manifest"]["settings"]["cpus"]
    prefix = ["taskset", "-c", ",".join(map(str, cpus))]
    selected = backend.from_settings(reference["manifest"]["settings"])
    settings = dict(interval_ps=20_000_000, thermal_threads=selected["solver_threads"],
                    thermal_backend=selected, native_cells=67139,
                    replay_simulated_ps=1_000_000_000)  # 1 ms, 50 thermal steps
    rows, bounds, signature = {}, None, None
    for timed in (True, False):
        name = "timing_on" if timed else "timing_off"
        folder = output / name
        started = time.monotonic_ns()
        env = capture_setup(folder / "capture", reference["workload"], reference["source"],
                            binary=binary, preload="", timed=timed)
        start, end = runner.execute([*prefix, "bash", str(runner.PROVIDER), "run"], env,
                                    folder / "capture/logs/simulator.log", 300)
        log = (folder / "capture/logs/simulator.log").read_text()
        require(log.count("CLO_SMOKE_PASS sum=49995000") == 1 and "CLO_SMOKE_FAIL" not in log,
                "capture smoke numerical validation failed")
        cycles = re.findall(r"Execution period is (\d+) ns", log)
        require(len(cycles) == 1 and (signature is None or signature == cycles[0]),
                "capture instrumentation changed simulated execution")
        signature = cycles[0]
        effective = runner.effective_configuration(folder / "capture", reference["workload"]["dramsys_path"], True)
        capture = None
        if timed:
            capture = summarize_capture(read_events(folder / "capture/timing"), start, end, settings["interval_ps"])
            bounds = capture["roi_start_ps"], capture["roi_end_ps"]
        record(folder / "capture", folder / "recording", *bounds)
        manifest, _, _ = load_recording(folder / "recording")
        replay_env = runner.environment(reference["workload"])
        timing = folder / "replay_timing"
        timing.mkdir()
        if timed:
            replay_env["CLO_TIMING_DIR"] = str(timing)
        start, end = runner.execute([*prefix, *replay_command(folder / "recording", folder / "replay", settings)],
                                    replay_env, folder / "replay.log", 300)
        summary = json.loads((folder / "replay/summary.json").read_text())
        row = dict(profile="temperature_aware", numerical_status="PASS", simulated_capture_ns=int(signature),
                   effective_capture=effective, recording_sha256=manifest["sha256"], replay_summary=summary)
        if timed:
            replay = summarize_replay(read_events(timing), start, end, summary, settings)
            row.update(capture=capture, replay=replay, combined=combine(capture, replay, started))
        rows[name] = row
    # Compare data, not gzip timestamps or elapsed-time metadata.
    paths = load_recording(output / "timing_on/recording")[0]["component_paths"]
    traces = [read_trace(output / name / "capture/traces/power_hook_trace.jsonl", paths)
              for name in ("timing_on", "timing_off")]
    for a, b in zip(*traces):
        np.testing.assert_array_equal(a, b)
    for filename in ("dynamic_power_w.csv.gz", "leakage_power_w.csv.gz", "component_temperatures_c.csv.gz"):
        with gzip.open(output / "timing_on/replay" / filename, "rb") as a, \
                gzip.open(output / "timing_off/replay" / filename, "rb") as b:
            require(a.read() == b.read(), f"instrumentation changed {filename}")
    with np.load(output / "timing_on/replay/final_state.npz") as a, \
            np.load(output / "timing_off/replay/final_state.npz") as b:
        require(set(a.files) == set(b.files), "final-state fields differ")
        for key in a.files:
            np.testing.assert_array_equal(a[key], b[key])
    result = dict(status="PASS", purpose="short native validation, not B8 or a 1-second benchmark",
                  capture_and_replay_numerically_identical=True, thermal_cells=67139, thermal_steps=50,
                  replay_simulated_seconds=.001, rows=rows)
    write_json(output / "validation.json", result)
    print(f"Capture/replay native validation passed: {output}", flush=True)
    return result
