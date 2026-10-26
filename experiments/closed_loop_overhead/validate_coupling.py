"""Short native equivalence checks and a synthetic 128-exchange transport benchmark.

No B8 workload is executed. Run this in isolation for meaningful host timings.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from .runner import ROOT, CONFIGS, command_for, execute, select_cpus, build_hook_client
from .smoke import prepare_smoke
from .analyze import read_events, summarize_events, partition, coupling_diagnostics
from thermal_manifest import copy_inputs
from system_contract import thermal_components


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def numerical_artifacts(directory):
    manifest = json.loads((directory / "results/3dice/thermal_manifest.json").read_text())
    files = [directory / "traces/3dice_power_traces.txt", directory / "traces/component_temperatures.csv"]
    files += [directory / "results/3dice" / die[key] for die in manifest["dies"]
              for key in ("temperature_output", "map_output")]
    return [p.read_bytes() for p in files]


def native_matrix(output):
    output, binary, env = prepare_smoke(output)
    cpus = ",".join(map(str, select_cpus()))
    runs, signatures = [], set()
    reference = None
    for order in ("dram_on_logic", "logic_on_dram"):
        thermal = json.loads((CONFIGS / "dram3d_thermal.json").read_text())
        thermal["stack_order"] = order
        config = output / f"{order}.json"
        save(config, thermal)
        for profile in ("temperature_aware", "constant"):
            artifacts = []
            for mode in ("legacy", "optimized"):
                directory = output / f"{order}_{profile}_{mode}"
                (directory / "logs").mkdir(parents=True)
                (directory / "timing").mkdir()
                run_env = dict(env, SOFTHIER_BINARY=str(binary), SIMULATOR_PLATFORM="",
                    SOFTHIER_RUN_CWD=str(directory / "results/simulator"),
                    SOFTHIER_THERMAL_CONFIG=str(config), SOFTHIER_POWER_PROFILE=profile,
                    CLO_TIMING_DIR=str(directory / "timing"))
                command = ["taskset", "-c", cpus, *command_for(100., directory, 20_000_000,
                                                              mode, profile=profile)]
                start, end = execute(command, run_env, directory / "logs/driver.log", 300)
                text = (directory / "logs/simulator.log").read_text()
                if text.count("CLO_SMOKE_PASS sum=49995000") != 1 or "CLO_SMOKE_FAIL" in text:
                    raise ValueError(f"native smoke numerical failure: {directory}")
                cycles = re.findall(r"Execution period is (\d+) ns", text)
                if len(cycles) != 1:
                    raise ValueError("missing native cycle signature")
                signatures.add(cycles[0])
                timing = summarize_events(read_events(directory / "timing"), start, end, True, 20_000_000)
                if timing["native_cells"] != 18107 or timing["thermal_steps"] != 4:
                    raise ValueError("unexpected native mesh or thermal step count")
                artifacts.append(numerical_artifacts(directory))
                record = dict(order=order, profile=profile, coupling_mode=mode, timing=timing,
                              simulated_ns=int(cycles[0]), directory=str(directory))
                save(directory / "validation.json", record)
                runs.append(record)
                if order == "dram_on_logic" and profile == "temperature_aware" and mode == "optimized":
                    reference = directory
            if artifacts[0] != artifacts[1]:
                raise ValueError(f"legacy/optimized power, feedback or thermal artifacts differ: {order}/{profile}")
    if len(signatures) != 1:
        raise ValueError("guest cycles changed across native comparisons")
    save(output / "native_equivalence.json", dict(status="PASS", purpose="short native kernel, not B8", runs=runs))
    return reference, env


def wait_ready(path, message, processes, timeout=60):
    deadline = time.monotonic() + timeout
    while not path.exists() or message not in path.read_text():
        if any(p.poll() is not None for p in processes) or time.monotonic() > deadline:
            raise RuntimeError(f"startup failed: {path}")
        time.sleep(.005)


def transport_case(reference, directory, mode, slots, base_env):
    directory.mkdir(parents=True)
    (directory / "timing").mkdir()
    source = reference / "results/3dice"
    copy_inputs(source / "thermal_manifest.json", directory / "thermal_manifest.json",
                source / "ice.stk", directory / "ice.stk")
    shutil.copy2(reference / "generated/system_config.json", directory / "system.json")
    document = json.loads((directory / "system.json").read_text())
    power = directory / "power.txt"
    power.touch()
    config = dict(hook=dict(name="3dice-gvsoc-power-hook", version=2),
        system_config=str(directory / "system.json"), thermal_manifest=str(directory / "thermal_manifest.json"),
        power_trace=str(power), temperature_history=str(directory / "history.csv"), default_power_w=None,
        poll_seconds=.005 if mode == "optimized" else .02, timeout_seconds=30.)
    config_path, request, response = [directory / name for name in ("hook.json", "request.json", "response.json")]
    save(config_path, config)
    env = dict(base_env, CLO_TIMING_DIR=str(directory / "timing"), POWER_HOOK_TIMEOUT_SECONDS="30")
    binary = build_hook_client()
    processes, logs, spans = [], [], []
    def launch(command, name):
        log = (directory / name).open("w"); logs.append(log)
        process = subprocess.Popen(command, cwd=directory, env=env, stdout=log, stderr=subprocess.STDOUT)
        processes.append(process)
        return process
    def call(phase, start_ps, end_ps, temperatures):
        samples = []
        if phase != "init":
            for index, component in enumerate(thermal_components(document)):
                temperature = temperatures.get(component["path"], 26.85)
                leakage = .001 * (1 + .01 * (temperature - 26.85))
                dynamic = .001 + index * .00001
                samples.append(dict(path=component["path"], scope="subtree", dynamic_w=dynamic,
                    leakage_w=leakage, total_w=dynamic + leakage, temperature_c=temperature))
        save(request, dict(protocol=dict(name="gvsoc-power-hook", version=1), phase=phase,
            temperature_unit="celsius", window=dict(start_ps=start_ps, end_ps=end_ps), components=samples))
        response.unlink(missing_ok=True)
        command = [str(binary)] if mode == "optimized" else [sys.executable,
            str(ROOT / "Interface_scripts/co-simulation/3dice_power_hook.py")]
        start = time.monotonic_ns()
        subprocess.run([*command, "--phase", phase, "--config", str(config_path),
            "--request", str(request), "--response", str(response)], env=env, check=True,
            stdout=subprocess.DEVNULL, timeout=35)
        end = time.monotonic_ns()
        if phase != "init":
            spans.append(dict(actor="producer", phase="exchange", pid=os.getpid(), start_ns=start,
                              end_ns=end, sim_ps=end_ps, value=end_ps-start_ps))
        return {item["path"]: item["temperature_c"] for item in json.loads(response.read_text())["temperatures"]}
    with tempfile.TemporaryDirectory(prefix="hook-check-") as socket_dir:
        try:
            server = launch([str(ROOT / "3D-ICE/bin/3D-ICE-Server"), "ice.stk", "--power-trace",
                str(power), "--follow", "--until-minus-one", "--poll", ".005" if mode == "optimized" else "1"],
                "server.log")
            wait_ready(directory / "server.log", "Running local power trace mode.", processes)
            if mode == "optimized":
                env["POWER_HOOK_SOCKET"] = str(Path(socket_dir) / "worker.sock")
                launch([sys.executable, str(ROOT / "Interface_scripts/co-simulation/power_hook_worker.py"),
                    "--socket", env["POWER_HOOK_SOCKET"], "--ready", str(directory / "ready.json"),
                    "--diagnostics", str(directory / "diagnostics.json"), "--config", str(config_path),
                    "--request", str(request), "--response", str(response), "--watch-pid", str(server.pid)], "worker.log")
                wait_ready(directory / "worker.log", "Power hook worker ready", processes)
            temperatures = call("init", 0, 0, {})
            for index in range(slots):
                final = index == slots - 1
                temperatures = call("final" if final else "update", index * 20_000_000,
                                    (index + 1) * 20_000_000 - (10_000_000 if final else 0), temperatures)
            for process in processes:
                if process.wait(timeout=10) != 0:
                    raise RuntimeError(f"transport process failed: {directory}")
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
            for log in logs:
                log.close()
    events = read_events(directory / "timing") + spans
    roi = spans[0]["start_ns"], spans[-1]["end_ns"]
    # Separate power mapping from the coordination category, without double counting.
    adjusted = [dict(e, phase="power_capture") if e["phase"] == "power_mapping" else e for e in events]
    phases = partition(adjusted, *roi, roi)
    if sum(e["phase"] == "thermal_step" for e in events) != slots:
        raise ValueError("thermal step count changed")
    save(directory / "exchange_spans.json", spans)
    result = dict(mode=mode, slots=slots, phases_seconds=phases,
                  coordination_seconds_per_exchange=phases["coupling_output"] / slots,
                  diagnostics=coupling_diagnostics(events, roi))
    save(directory / "measurement.json", result)
    return result


def run(output, slots=128):
    output = Path(output).resolve()
    if slots < 128:
        raise ValueError("transport validation requires at least 128 exchanges")
    os.sched_setaffinity(0, select_cpus())
    reference, env = native_matrix(output)
    cases = [transport_case(reference, output / f"transport_{mode}", mode, slots, env)
             for mode in ("legacy", "optimized")]
    manifest = json.loads((reference / "results/3dice/thermal_manifest.json").read_text())
    names = ["power.txt", "history.csv"] + [die[key] for die in manifest["dies"]
                                            for key in ("temperature_output", "map_output")]
    for name in names:
        if (output / "transport_legacy" / name).read_bytes() != (output / "transport_optimized" / name).read_bytes():
            raise ValueError(f"transport numerical output differs: {name}")
    result = dict(status="PASS" if cases[1]["coordination_seconds_per_exchange"] < .1 else "TARGET_MISSED",
                  purpose="short native equivalence and synthetic transport; not B8 performance",
                  cases=cases, optimized_under_100ms=cases[1]["coordination_seconds_per_exchange"] < .1,
                  coordination_reduction_percent=100 * (1 - cases[1]["coordination_seconds_per_exchange"] /
                                                           cases[0]["coordination_seconds_per_exchange"]))
    save(output / "validation.json", result)
    if not result["optimized_under_100ms"]:
        raise RuntimeError("numerical checks passed but the 100 ms coordination target was missed")
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--slots", type=int, default=128)
    args = parser.parse_args()
    run(args.output, args.slots)
