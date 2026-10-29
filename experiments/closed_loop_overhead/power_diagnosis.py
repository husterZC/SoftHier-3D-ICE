"""Bounded B8 comparisons of inline logic and DRAM power accounting.

Reuse the recorded B8 executable/configuration. The first window is timed
without sampling; the immediately following window is sampled separately.
These are fixed-temperature, partial-workload diagnostics, not full B8 results.
"""
import argparse
import copy
import csv
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import time

from .runner import ROOT, select_cpus, no_power_environment
from .native_profile import Sampler, symbolize
from .analyze import power_accounting_counter, read_events

MODES = {"architecture_only": (False, False), "logic_only": (True, False),
         "dram_only": (False, True), "logic_and_dram": (True, True)}


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def wait_marker(process, directory, phase, timeout):
    deadline = time.monotonic() + timeout
    while True:
        path = directory / "diagnostic_events.jsonl"
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record["phase"] == phase:
                    state = Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
                    if state in ("T", "t"):
                        return record
        if process.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError(f"failed waiting for {phase}; see {directory}/simulator.log")
        time.sleep(.05)


def build_launcher(output):
    install = ROOT / "SoftHier/.power_interface/install"
    native = json.loads((install / "native_build.json").read_text())
    include = Path(native["staged_source"]) / "engine/engine/include"
    systemc = install.parent / "dependencies/systemc-install"
    binary = output / "diagnostic_launcher"
    compiler = shutil.which("g++-14.2.0") or shutil.which("g++")
    # Match gvsoc_launcher_profile_sc, including layout-affecting definitions.
    command = [compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Wno-unused-parameter",
               "-UNDEBUG", "-DVP_USE_SYSTEMC=1", "-DCONFIG_GVSOC_EVENT_ACTIVE=1",
               "-DVP_TRACE_ACTIVE=1", "-DCONFIG_GVSOC_STATS_ACTIVE=1", "-DVP_ASSERT_ACTIVE=1",
               "-I" + str(include), "-I" + str(systemc / "include"),
               str(Path(__file__).with_name("diagnostic_launcher.cpp")),
               "-L" + str(install / "lib"), "-L" + str(systemc / "lib64"),
               "-lpulpvp-profile", "-lsystemc", "-pthread", "-ldl", "-o", str(binary)]
    save(output / "launcher_build.json", dict(command=command, staged_source=native["staged_source"]))
    result = subprocess.run(command, capture_output=True, text=True)
    (output / "launcher_build.log").write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f"diagnostic launcher build failed: {output}/launcher_build.log")
    return binary


def native_environment(workload, dram_path):
    install = ROOT / "SoftHier/.power_interface/install"
    dependencies = install.parent / "dependencies"
    compiler = shutil.which("g++-14.2.0") or shutil.which("g++")
    runtime = Path(subprocess.check_output([compiler, "-print-file-name=libstdc++.so"], text=True).strip()).parent
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLO_", "POWER_HOOK_", "SOFTHIER_"))}
    env.update(DRAMSYS_PATH=str(dram_path), SOFTHIER_DRAM_POWER_CONFIG=workload["dram_power_config"],
               SOFTHIER_POWER_PROFILE="temperature_aware", OMP_NUM_THREADS="1",
               OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1",
               LD_LIBRARY_PATH=os.pathsep.join(map(str, [install / "lib", dependencies / "systemc-install/lib64",
                                                        dependencies / "dramsys-install", runtime])) +
                               os.pathsep + os.environ.get("LD_LIBRARY_PATH", ""))
    return env


def run_case(directory, template, workload, mode, start_ps, window_ps, cpus, timeout, sample, launcher,
             power_timing=True):
    directory.mkdir()
    (directory / "timing").mkdir()
    logic, dram = MODES[mode]
    dram_path = directory / "dram_environment"
    if dram:
        shutil.copytree(workload["dramsys_path"], dram_path)
    else:
        no_power_environment(workload["dramsys_path"], dram_path)
    config = copy.deepcopy(template)
    gv = config["target"]["gvsoc"]
    assert gv["profile-mode"] and not gv["debug-mode"] and not gv["events"]["enabled"]
    assert not gv["power_hook"]["executable"]
    gv["power"] = logic
    gv["proxy"]["enabled"] = False
    save(directory / "gvsoc_config.json", config)
    env = native_environment(workload, dram_path)
    env["CLO_TIMING_DIR"] = str(directory / "timing")
    env["CLO_POWER_ACCOUNTING_TIMING"] = str(int(power_timing))
    env["CLO_POWER_TIMING_EXTERNAL_WINDOW"] = "1"
    command = ["taskset", "-c", ",".join(map(str, cpus)), "stdbuf", "-oL", "-eL",
               str(launcher), "gvsoc_config.json", str(start_ps), str(window_ps)]
    record = dict(mode=mode, logic_power=logic, dram_power=dram, status="running",
                  power_accounting_timing=power_timing, directory=str(directory),
                  start_ps=start_ps, window_ps=window_ps, command=command, cpus=cpus,
                  scope="bounded B8; fixed initial power-model temperature; no hook or thermal solver")
    save(directory / "measurement.json", record)
    sampler = None
    with (directory / "simulator.log").open("w") as log:
        process = subprocess.Popen(command, cwd=directory, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            print(f"{mode}: advancing to {start_ps / 1e6:g} us", flush=True)
            ready = wait_marker(process, directory, "ready", timeout)
            assert ready["sim_ps"] == start_ps
            process.send_signal(signal.SIGCONT)
            end = wait_marker(process, directory, "window_end", timeout)
            marks = [json.loads(line) for line in (directory / "diagnostic_events.jsonl").read_text().splitlines()]
            begin = next(m for m in marks if m["phase"] == "window_start")
            assert end["sim_ps"] - begin["sim_ps"] == window_ps
            record.update(wall_seconds=(end["host_ns"] - begin["host_ns"]) * 1e-9,
                          cpu_seconds=(end["cpu_us"] - begin["cpu_us"]) * 1e-6,
                          end_ps=end["sim_ps"], clock_cycles=end["clock_cycles"])
            record["power_accounting"] = power_accounting_counter(read_events(directory / "timing"),
                (begin["host_ns"], end["host_ns"]), required=power_timing, scope="diagnostic_window")
            if not power_timing and record["power_accounting"] is not None:
                raise ValueError("direct timer unexpectedly enabled")
            print(f"{mode}: unsampled window {record['wall_seconds']:.3f} host seconds", flush=True)
            if sample:
                sampler = Sampler(process.pid)
                sampler.start()
            process.send_signal(signal.SIGCONT)
            profile_end = wait_marker(process, directory, "profile_end", timeout)
            assert profile_end["sim_ps"] == start_ps + 2 * window_ps
            if sample:
                sampler.stop()
                maps = Path(f"/proc/{process.pid}/maps").read_text()
                (directory / "profile.maps").write_text(maps)
                save(directory / "samples.json", [dict(pid=pid, tid=tid, ip=ip, count=count)
                     for (pid, tid, ip), count in sampler.samples.items()])
                report = symbolize(sampler.samples, maps)
                report.update(lost_samples=sampler.lost, start_ps=start_ps + window_ps,
                              end_ps=start_ps + 2 * window_ps)
                if not report["sample_count"] or sampler.lost:
                    raise RuntimeError("sampling produced no data or lost records")
                save(directory / "profile.json", report)
                record["sample_count"] = report["sample_count"]
            process.send_signal(signal.SIGCONT)
            code = process.wait(timeout=30)
            if code:
                raise RuntimeError(f"simulator exited with {code}")
            record.update(status="complete_window", returncode=code,
                          full_workload_validation="not reached: intentionally bounded")
        except BaseException as error:
            record.update(status="failed", error=f"{type(error).__name__}: {error}")
            raise
        finally:
            if sampler:
                sampler.close()
            if process.poll() is None:
                process.terminate()
                process.send_signal(signal.SIGCONT)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
            save(directory / "measurement.json", record)
    return record


def analyze_windows(output, rows):
    result = dict(status="complete", runs=rows, scope="bounded diagnostic, not full-workload numerical validation")
    by = {r["mode"]: r for r in rows}
    if len(rows) == 4:
        assert all(r["clock_cycles"] == rows[0]["clock_cycles"] for r in rows), "clock-cycle state changed"
        base = by["architecture_only"]["wall_seconds"]
        result["overhead_seconds"] = {k: r["wall_seconds"] - base for k, r in by.items()}
        result["interaction_seconds"] = (by["logic_and_dram"]["wall_seconds"] - by["logic_only"]["wall_seconds"] -
                                         by["dram_only"]["wall_seconds"] + base)
    profiles, hotspots = {}, []
    for row in rows:
        directory = Path(row.get("directory", output / row["mode"]))
        if not (directory / "profile.json").exists():
            continue
        report = json.loads((directory / "profile.json").read_text())
        marks = {m["phase"]: m for m in map(json.loads, (directory / "diagnostic_events.jsonl").read_text().splitlines())}
        begin, end = marks["profile_start"], marks["profile_end"]
        cpu = (end["cpu_us"] - begin["cpu_us"]) * 1e-6
        profiles[row["mode"]] = dict(cpu_seconds=cpu, wall_seconds=(end["host_ns"] - begin["host_ns"]) * 1e-9,
                                    sample_count=report["sample_count"], lost_samples=report["lost_samples"],
                                    categories=report["categories"], method=report["method"])
        for function in report["functions"]:
            hotspots.append(dict(mode=row["mode"], **function,
                                 estimated_profile_cpu_seconds=cpu * function["percent"] / 100))
    result["profiles"] = profiles
    with (output / "summary.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["mode", "power_accounting_timing", "wall_seconds", "cpu_seconds", "sample_count"])
        writer.writeheader()
        writer.writerows({k: row.get(k, "") for k in writer.fieldnames} for row in rows)
    if hotspots:
        with (output / "hotspots.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(hotspots[0]))
            writer.writeheader()
            writer.writerows(hotspots)
    save(output / "analysis.json", result)
    return result


def analyze_timer_calibration(output, rows):
    comparison = {}
    for mode in sorted({row["mode"] for row in rows}):
        pair = {r["power_accounting_timing"]: r for r in rows if r["mode"] == mode}
        if len(pair) != 2 or len([r for r in rows if r["mode"] == mode]) != 2:
            raise ValueError("calibration requires one matched timer-on/off pair per mode")
        off, on = pair[False], pair[True]
        if (off["clock_cycles"] != on["clock_cycles"] or off["start_ps"] != on["start_ps"] or
                off["end_ps"] != on["end_ps"]):
            raise ValueError("timer calibration changed architectural progress")
        comparison[mode] = dict(off_seconds=off["wall_seconds"], on_seconds=on["wall_seconds"],
            observed_overhead_percent=100 * (on["wall_seconds"] / off["wall_seconds"] - 1),
            inline_power_seconds=on["power_accounting"]["seconds"],
            outer_calls=on["power_accounting"]["calls"])
    result = dict(status="PASS", comparison=comparison, runs=rows,
        scope="matched bounded B8 windows; timer on/off, no statistical sampling, not full B8 validation",
        interpretation="One pair per mode; observed differences include host noise. No correction is applied to measured durations.")
    save(output / "analysis.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-study", type=Path, default=Path(__file__).parent / "results/b8_overhead_optimized")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start-us", type=float, default=1000.)
    parser.add_argument("--window-us", type=float, default=100.)
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--cpus")
    parser.add_argument("--timeout", type=float, default=900.)
    parser.add_argument("--no-sampling", action="store_true")
    parser.add_argument("--power-accounting-timing", action="store_true", default=True,
                        help="Direct inline-power timing (always enabled outside calibration)")
    parser.add_argument("--calibrate-power-timing", action="store_true",
                        help="Run matched timer-off/on windows per mode without sampling")
    args = parser.parse_args()
    if not (0 <= args.start_us < 25000 and 0 < args.window_us and
            args.start_us + 2 * args.window_us < 25000):
        parser.error("windows must be positive and end before 25 ms of B8")
    if args.timeout <= 0 or len(set(args.modes)) != len(args.modes):
        parser.error("timeout must be positive and modes must be unique")
    output, reference = args.output.resolve(), args.reference_study.resolve()
    output.mkdir(parents=True, exist_ok=False)
    workload = json.loads((reference / "workload/workload.json").read_text())
    case = workload["cases"]["decode_b8_kv1024"]
    for name in ("binary", "preload"):
        if hashlib.sha256(Path(case[name]).read_bytes()).hexdigest() != case[name + "_sha256"]:
            raise ValueError(f"reference {name} hash changed")
    template = json.loads((reference / "runs/block01_architecture_only/results/simulator/gvsoc_config.json").read_text())
    cpus = select_cpus(args.cpus)
    launcher = build_launcher(output)
    rows = []
    study = dict(status="running", arguments=vars(args) | {
        "reference_study": str(reference), "output": str(output)}, cpus=cpus,
        binary_sha256=case["binary_sha256"], preload_sha256=case["preload_sha256"])
    artifacts = [Path(__file__), Path(__file__).with_name("diagnostic_launcher.cpp"),
                 Path(__file__).with_name("native_profile.py"), launcher,
                 ROOT / "SoftHier/.power_interface/install/native_build.json",
                 ROOT / "SoftHier/.power_interface/install/lib/libpulpvp-profile.so"]
    study["artifacts_sha256"] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in artifacts}
    snapshot = output / "source_snapshot"
    snapshot.mkdir()
    for path in artifacts[:3]:
        shutil.copy2(path, snapshot / path.name)
    save(output / "study.json", study)
    try:
        rng = random.Random(19)
        for mode in args.modes:
            switches = [False, True] if args.calibrate_power_timing else [args.power_accounting_timing]
            rng.shuffle(switches)
            for enabled in switches:
                name = mode + ("_timing_on" if enabled else "_timing_off") if args.calibrate_power_timing else mode
                rows.append(run_case(output / name, template, workload, mode, round(args.start_us * 1e6),
                    round(args.window_us * 1e6), cpus, args.timeout,
                    not (args.no_sampling or args.calibrate_power_timing), launcher, enabled))
                save(output / "results.json", rows)
        result = analyze_timer_calibration(output, rows) if args.calibrate_power_timing else analyze_windows(output, rows)
        study["status"] = "complete"
    except BaseException as error:
        study.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        save(output / "study.json", study)
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
