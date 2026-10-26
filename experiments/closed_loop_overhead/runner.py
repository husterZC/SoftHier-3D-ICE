"""Four-configuration, randomized-block B8 benchmark."""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import resource
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PROVIDER = ROOT / "Interface_scripts/providers/softhier/provider.sh"
CONFIGS = PROVIDER.parent / "configs"
CASE = "decode_b8_kv1024"
sys.path.insert(0, str(ROOT / "experiments/llm_dram3d"))
sys.path.insert(0, str(ROOT / "Interface_scripts/co-simulation"))
from build_hook_client import build as build_hook_client
from prepare_workload import (prepare, build, load_sdk_report, sha256, write_json, validate_inputs,
                              source_hashes as workload_source_hashes)
from run_study import parse_timing
from .analyze import analyze, read_events, summarize_events


def positive_int(value):
    value = int(value)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def positive_float(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("analyze", help="Analyze completed blocks without running simulators")
    report.add_argument("study", type=Path)
    smoke = commands.add_parser("smoke", help="Run a short native kernel, not the B8 study")
    smoke.add_argument("--output", type=Path, required=True)
    smoke.add_argument("--replicates", type=positive_int, default=3)
    smoke.add_argument("--coupling-mode", choices=("optimized", "legacy"), default="optimized")
    for command in ("plan", "run"):
        p = commands.add_parser(command, help="Print configuration only" if command == "plan" else "Execute the study")
        p.add_argument("--prefix", default=datetime.now(timezone.utc).strftime("b8_%Y%m%dT%H%M%SZ"))
        p.add_argument("--grid-pitches-um", nargs=3, type=positive_float, default=[100., 50., 20.])
        p.add_argument("--replicates", type=positive_int, default=5)
        p.add_argument("--interval-ps", type=positive_int, default=20_000_000)
        p.add_argument("--coupling-mode", choices=("optimized", "legacy"), default="optimized")
        p.add_argument("--dice-poll-seconds", type=positive_float)
        p.add_argument("--adapter-poll-seconds", type=positive_float)
        p.add_argument("--seed", type=int, default=19, help="Workload and randomized schedule seed")
        p.add_argument("--layer", type=int, default=1)
        p.add_argument("--cpus", help="Comma-separated allowed CPUs; default four physical cores on one socket")
        p.add_argument("--thermal-config", type=Path, default=CONFIGS / "dram3d_thermal.json")
        p.add_argument("--dram-power-config", type=Path, default=CONFIGS / "dram3d_power.json")
        p.add_argument("--workload", type=Path, help="Reuse a compatible previously prepared workload.json")
        p.add_argument("--build-hardware", action="store_true", help="Build native dependencies/GVSoC and 3D-ICE before pilots")
        p.add_argument("--resume", action="store_true", help="Resume an identical study, retaining failed attempts")
        p.add_argument("--max-host-seconds", type=positive_float, help="Optional per-run timeout; failures are excluded")
    args = parser.parse_args(argv)
    if args.command in ("plan", "run"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.prefix):
            parser.error("prefix must be a simple run name")
        if len(set(args.grid_pitches_um)) != 3:
            parser.error("three distinct pitches are required")
        if args.seed < 0 or not 0 <= args.layer < 36:
            parser.error("seed must be nonnegative and layer must be in [0,35]")
    return args


def select_cpus(spec=None):
    allowed = os.sched_getaffinity(0)
    if spec:
        chosen = sorted(set(map(int, spec.split(","))))
        if not chosen or not set(chosen) <= allowed:
            raise ValueError("requested CPUs are outside the process affinity")
        return chosen
    sockets = {}
    for cpu in sorted(allowed):
        topology = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        socket = (topology / "physical_package_id").read_text().strip()
        nodes = sorted(p.name for p in topology.parent.glob("node[0-9]*"))
        socket = (socket, tuple(nodes))
        core = (topology / "core_id").read_text().strip()
        sockets.setdefault(socket, {}).setdefault(core, cpu)
    cores = max(sockets.values(), key=len)
    return list(cores.values())[:4]


def settings(args):
    return dict(case=CASE, layer=args.layer, seed=args.seed, workload_repeats=1,
                floorplan="square_bands", profile="temperature_aware",
                grid_pitches_um=args.grid_pitches_um, replicates=args.replicates,
                interval_ps=args.interval_ps, thermal_step_seconds=args.interval_ps * 1e-12,
                cpus=select_cpus(args.cpus), thermal_threads=1, coupling_mode=args.coupling_mode,
                adapter_poll_seconds=args.adapter_poll_seconds or (.005 if args.coupling_mode == "optimized" else .02),
                server_poll_seconds=args.dice_poll_seconds or (.005 if args.coupling_mode == "optimized" else 1.),
                thermal_config=str(args.thermal_config.resolve()),
                dram_power_config=str(args.dram_power_config.resolve()),
                output_policy="region_temperatures_each_slot_and_final_maps",
                max_host_seconds=args.max_host_seconds)


def schedule(config, seed, replicates):
    rng = random.Random(seed)
    result = []
    for block in range(replicates + 1):
        order = list(config)
        rng.shuffle(order)
        for name in order:
            result.append(dict(id=f"{'pilot' if block == 0 else f'block{block:02d}'}_{name}",
                               configuration=name, block=block, pilot=block == 0))
    return result


def configurations(pitches):
    return dict(architecture_only=None, **{f"closed_loop_{p:g}": p for p in pitches})


def capture(command, cwd=ROOT):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    return dict(command=list(map(str, command)), returncode=result.returncode,
                stdout=result.stdout.strip(), stderr=result.stderr.strip())


def host_info():
    return dict(platform=platform.platform(), python=sys.version, executable=sys.executable,
                affinity=sorted(os.sched_getaffinity(0)), cpu=capture(["lscpu", "-J"]),
                compilers={name: capture([name, "--version"]) for name in
                           ("gcc", "g++", "gcc-14.2.0", "g++-14.2.0") if shutil.which(name)},
                compiler_environment={key: os.environ.get(key) for key in ("CC", "CXX", "CFLAGS", "CXXFLAGS")},
                memory=Path("/proc/meminfo").read_text(),
                cgroup=Path("/proc/self/cgroup").read_text(),
                limits={str(p): p.read_text().strip() for p in
                        (Path("/sys/fs/cgroup/cpu.max"), Path("/sys/fs/cgroup/memory.max"),
                         Path("/sys/fs/cgroup/cpuset.cpus.effective"), Path("/sys/fs/cgroup/cpuset.mems.effective"),
                         Path("/sys/devices/system/cpu/intel_pstate/no_turbo")) if p.is_file()},
                cpu_governors={str(p): p.read_text().strip() for p in
                    Path("/sys/devices/system/cpu").glob("cpu*/cpufreq/scaling_governor")},
                revisions={name: capture(["git", "rev-parse", "HEAD"], ROOT / name)
                           for name in (".", "SoftHier", "SoftHier/engine", "SoftHier/pulp",
                                        "SoftHier/core", "SoftHier/soft_hier_sdk", "3D-ICE")})


def source_files():
    files = [ROOT / "Makefile", ROOT / "3D-ICE/bin/3D-ICE-Server.c",
             ROOT / "3D-ICE/sources/thermal_data.c", ROOT / "3D-ICE/include/closed_loop_timing.h",
             ROOT / "3D-ICE/bin/3D-ICE-Client.c", ROOT / "3D-ICE/bin/power_trace_reader.c",
             ROOT / "3D-ICE/bin/power_trace_reader.h",
             ROOT / "Interface_scripts/overhead_timing.py", ROOT / "Interface_scripts/system_contract.py",
             ROOT / "Interface_scripts/thermal_manifest.py"]
    for directory in (HERE, PROVIDER.parent, ROOT / "Interface_scripts/geometry_generator",
                      ROOT / "Interface_scripts/co-simulation", ROOT / "experiments/llm_dram3d"):
        files.extend(p for p in directory.rglob("*") if p.is_file()
                     and not {"results", "workloads", "logs", "__pycache__"}.intersection(p.parts)
                     and p.suffix in (".py", ".sh", ".c", ".h", ".hpp", ".patch", ".json", ".md"))
    return sorted(set(files))


def source_hashes():
    return {str(p.relative_to(ROOT)): sha256(p) for p in source_files()}


def no_power_environment(source, destination):
    shutil.copytree(source, destination)
    # Only the selected HBM2 settings are active. Unrelated DRAMSys examples
    # include relaxed JSON and must neither be parsed nor modified here.
    base = destination / "dramsys_configs"
    simulation = json.loads((base / "hbm2-example.json").read_text())
    path = (base / simulation["simulation"]["simconfig"]).resolve()
    path.relative_to(base.resolve())
    document = json.loads(path.read_text())
    document["simconfig"]["PowerAnalysis"] = False
    write_json(path, document)
    return str(destination)


def environment(workload):
    # Keep toolchain/environment activation, but isolate experiment-owned knobs.
    prefixes = ("SOFTHIER_", "SIMULATOR_", "ICE_", "DICE_", "POWER_HOOK_", "RUN_", "CLO_", "COUPLING_")
    paths = {"ROOT_DIR", "GENERATED_DIR", "TRACE_DIR", "RESULT_DIR", "STATE_DIR", "LOG_DIR", "PID_DIR",
             "SYSTEM_CONFIG_FILE", "GEO_FILE", "DEFAULT_POWER_W", "OTHERS_POWER", "PWR_INTERVAL_PS"}
    base = {k: v for k, v in os.environ.items() if not k.startswith(prefixes) and k not in paths}
    env = dict(base, PYTHON=sys.executable, PYTHONDONTWRITEBYTECODE="1", SOFTHIER_CONDA_ENV="",
               PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
               ROOT_DIR=str(ROOT), SOFTHIER_DIR=str(ROOT / "SoftHier"),
               SOFTHIER_WORKDIR=str(ROOT / "SoftHier/.power_interface"),
               SOFTHIER_TARGET="pulp.chips.soft_hier_old.flex_cluster",
               SIMULATOR_CONFIG=workload["arch"], SIMULATOR_APP=workload["source"],
               SOFTHIER_FLOORPLAN="square_bands", SOFTHIER_PRELOAD_MODE="direct",
               SOFTHIER_CORE_MODEL="fast", SOFTHIER_POWER_PROFILE="temperature_aware",
               SOFTHIER_INPUT_PRELOAD="", SOFTHIER_THERMAL_CONFIG=workload["thermal_config"],
               SOFTHIER_DRAM_POWER_CONFIG=workload["dram_power_config"],
               SOFTHIER_DRAMSYS_PATH=workload["dramsys_path"],
               SIMULATOR_PROVIDER=str(PROVIDER), SOFTHIER_PROVIDER_SCRIPT_DIR=str(PROVIDER.parent),
               OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1",
               COUPLING_MODE="optimized", POWER_HOOK_POLL_SECONDS="0.005", DICE_POLL_SECONDS="0.005",
               AUTO_BOOTSTRAP="0", ICE_GENERATE_GIF="0", DICE_RUN_MODE="local-server")
    return env


def native_artifacts():
    install = ROOT / "SoftHier/.power_interface/install"
    controllers = sorted((install / "models").rglob("gen_pulp_chips_soft_hier_old_ctrl_registers_cpp_*.so"))
    if not controllers or any(b"roi_start" not in p.read_bytes() for p in controllers):
        raise ValueError("native ROI instrumentation is missing; use --build-hardware")
    return [ROOT / "3D-ICE/bin/3D-ICE-Server", install / "native_build.json",
            ROOT / "SoftHier/.power_interface/dependencies/dramsys-install/native_build.json",
            install / "lib/libpulpvp.so", install / "lib/libpulpvp-profile.so", *controllers,
            ROOT / "3D-ICE/bin/3D-ICE-Client", build_hook_client()]


def command_for(pitch, run_dir, interval, coupling_mode="optimized", server_poll=None, adapter_poll=None, profile="temperature_aware"):
    if pitch is None:
        return ["bash", str(PROVIDER), "run-uncoupled"]
    seconds = f"{interval * 1e-12:.12g}"
    return ["make", "coupled-run", "RUN_NAME=closed_loop_overhead", f"RUN_DIR={run_dir}",
            "BUILD_SIMULATOR=0", "BUILD_3DICE=0", f"SOFTHIER_POWER_PROFILE={profile}",
            f"COUPLING_MODE={coupling_mode}",
            f"DICE_POLL_SECONDS={server_poll if server_poll is not None else (.005 if coupling_mode == 'optimized' else 1.)}",
            f"POWER_HOOK_POLL_SECONDS={adapter_poll if adapter_poll is not None else (.005 if coupling_mode == 'optimized' else .02)}",
            f"POWER_INTERVAL_PS={interval}", f"ICE_SLOT_SECONDS={seconds}", f"ICE_STEP_SECONDS={seconds}",
            f"ICE_GRID_PITCH_UM={pitch:g}", "ICE_TARGET_TOP_DIE_CELLS=", "ICE_MAP_OUTPUT=final",
            "DICE_RUN_MODE=local-server", "ICE_GENERATE_GIF=0", "WAIT_TIMEOUT=3600"]


def execute(command, env, log, timeout=None):
    print(f"Running {' '.join(map(str, command))}\n  log: {log}", flush=True)
    with log.open("w") as stream:
        usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        start = time.monotonic_ns()
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=stream,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while True:
                remaining = None if timeout is None else timeout - (time.monotonic_ns() - start) * 1e-9
                if remaining is not None and remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    status = process.wait(timeout=min(30, remaining) if remaining is not None else 30)
                    break
                except subprocess.TimeoutExpired:
                    print(f"  still running: {(time.monotonic_ns() - start) / 1e9:.0f} host seconds", flush=True)
            end = time.monotonic_ns()
            if status:
                raise subprocess.CalledProcessError(status, command)
            return start, end
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL); process.wait()
            usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
            log.with_suffix(".resources.json").write_text(json.dumps(dict(
                process_tree_user_seconds=usage_after.ru_utime - usage_before.ru_utime,
                process_tree_system_seconds=usage_after.ru_stime - usage_before.ru_stime,
                returncode=process.returncode), indent=2) + "\n")


def effective_configuration(run_dir, dramsys_path, thermal):
    config = json.loads((run_dir / "results/simulator/gvsoc_config.json").read_text())
    effective = {k: config["target"]["gvsoc"].get(k) for k in ("power", "debug-mode", "profile-mode")}
    loaded = {Path(p).resolve() for p in re.findall(r"DRAMSYS_PATH: (.+?) \(from env\)",
              (run_dir / "logs/simulator.log").read_text())}
    if loaded != {Path(dramsys_path).resolve()}:
        raise ValueError("native DRAMSys loaded a different configuration directory")
    base = Path(dramsys_path) / "dramsys_configs"
    selected = json.loads((base / "hbm2-example.json").read_text())["simulation"]["simconfig"]
    settings_path = (base / selected).resolve()
    settings_path.relative_to(base.resolve())
    power = json.loads(settings_path.read_text())["simconfig"]["PowerAnalysis"]
    if effective["power"] is not thermal or power is not thermal:
        raise ValueError("effective architecture/DRAM power flags differ from the requested configuration")
    return dict(gvsoc=effective, dram_power_analysis=power, dram_simconfig_sha256=sha256(settings_path),
                dramsys_path=str(Path(dramsys_path).resolve()))


def compatible_workload(workload, config):
    if (workload.get("plan_only") or workload.get("layer") != config["layer"]
            or workload.get("seed") != config["seed"] or workload.get("repeats", 1) != 1
            or workload.get("all_experts") or CASE not in workload.get("cases", {})):
        raise ValueError("workload does not match this B8 experiment")
    for key in ("thermal_config", "dram_power_config"):
        if sha256(workload[key]) != sha256(config[key]):
            raise ValueError(f"reused workload has different {key}")
    if sha256(workload["arch"]) != workload["arch_sha256"]:
        raise ValueError("workload architecture hash mismatch")
    if workload_source_hashes(Path(workload["source"])) != workload["source_sha256"]:
        raise ValueError("workload source hash mismatch")
    case = workload["cases"][CASE]
    for key in ("binary", "preload"):
        if key in case and sha256(case[key]) != case[key + "_sha256"]:
            raise ValueError(f"reused {key} hash mismatch")


def run(args):
    config = settings(args)
    modes = configurations(args.grid_pitches_um)
    directory = HERE / "results" / args.prefix
    if args.command == "plan":
        print(json.dumps(dict(directory=str(directory), settings=config, configurations=modes,
                              schedule=schedule(modes, args.seed, args.replicates)), indent=2))
        return
    validate_inputs(args.thermal_config, args.dram_power_config)
    directory.parent.mkdir(parents=True, exist_ok=True)
    with (directory.parent / ".runner.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_locked(args, directory, config, modes)


def run_locked(args, directory, config, modes):
    hashes = source_hashes()
    if args.resume:
        manifest = json.loads((directory / "study.json").read_text())
        if manifest["settings"] != config or manifest["source_sha256"] != hashes:
            raise ValueError("resume requires identical settings and source files")
    else:
        directory.mkdir(exist_ok=False)
        manifest = dict(schema_version=1, settings=config, configurations=modes, host=host_info(),
                        source_sha256=hashes, schedule=schedule(modes, args.seed, args.replicates),
                        status="preparing", created_utc=datetime.now(timezone.utc).isoformat())
        for relative in hashes:
            dest = directory / "source_snapshot" / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, dest)
        write_json(directory / "study.json", manifest)
    if "workload" in manifest:
        workload = json.loads((directory / "workload_reference.json").read_text())
    elif args.resume and (directory / "workload/workload.json").is_file():
        workload = json.loads((directory / "workload/workload.json").read_text())
    elif args.workload:
        workload = json.loads(args.workload.resolve().read_text())
    else:
        with (directory / "prepare.log").open("w") as log:
            workload = prepare(ROOT / "SoftHier/soft_hier_sdk", directory / "workload", [CASE],
                               args.thermal_config, args.dram_power_config,
                               layer=args.layer, seed=args.seed, repeats=1, log=log)
    compatible_workload(workload, config)
    env = environment(workload)
    if args.build_hardware:
        execute(["bash", str(PROVIDER), "build-hardware"], env, directory / "hardware_build.log")
        execute(["make", "-C", str(ROOT / "3D-ICE"), "-j4"], env, directory / "ice_build.log")
    if "binary" not in workload["cases"][CASE]:
        with (directory / "software_build.log").open("w") as log:
            workload = build(workload, log=log)
    compatible_workload(workload, config)
    write_json(directory / "workload_reference.json", workload)
    manifest["workload"] = "workload_reference.json"
    baseline = directory / "architecture_environment"
    if not baseline.exists():
        no_power_environment(workload["dramsys_path"], baseline)
    environments = {"architecture_only": workload_source_hashes(baseline),
                    "closed_loop": workload_source_hashes(Path(workload["dramsys_path"]))}
    if "environment_sha256" in manifest and manifest["environment_sha256"] != environments:
        raise ValueError("DRAM configuration files changed; start a new study")
    manifest["environment_sha256"] = environments
    case = workload["cases"][CASE]
    manifest["binary_sha256"] = case["binary_sha256"]
    manifest["preload_sha256"] = case["preload_sha256"]
    artifacts = native_artifacts()
    current = {str(p.relative_to(ROOT)): sha256(p) for p in artifacts}
    if "native_artifact_sha256" in manifest and manifest["native_artifact_sha256"] != current:
        raise ValueError("native artifacts changed; start a new study")
    manifest["native_artifact_sha256"] = current
    for p in artifacts[1:]:
        if p.suffix != ".json":
            continue
        destination = directory / "source_snapshot" / p.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, destination)
    if b"CLO_TIMING_DIR" not in artifacts[0].read_bytes():
        raise ValueError("3D-ICE lacks optional timing probes; use --build-hardware")
    execute(["bash", str(PROVIDER), "check"], env, directory / "provider_check.log")
    execute([sys.executable, str(PROVIDER.parent / "stage_native.py"), "--action", "check",
             "--source", str(ROOT / "SoftHier"), "--workdir", str(ROOT / "SoftHier/.power_interface"),
             "--target", env["SOFTHIER_TARGET"], "--systemc-home",
             str(ROOT / "SoftHier/.power_interface/dependencies/systemc-install"),
             "--systemc-version", "3.0.1"], env, directory / "native_check.log")
    manifest["status"] = "running"
    write_json(directory / "study.json", manifest)
    for item in manifest["schedule"]:
        run_dir = directory / "runs" / item["id"]
        record_path = run_dir / "measurement.json"
        if record_path.exists() and json.loads(record_path.read_text())["status"] == "complete":
            continue
        if source_hashes() != hashes or any(sha256(p) != current[str(p.relative_to(ROOT))] for p in artifacts):
            raise ValueError("sources or native artifacts changed during the study")
        compatible_workload(workload, config)
        if (workload_source_hashes(baseline) != environments["architecture_only"] or
                workload_source_hashes(Path(workload["dramsys_path"])) != environments["closed_loop"]):
            raise ValueError("DRAM configuration changed during the study")
        if run_dir.exists():
            # Preserve interrupted/failed work, including a missing measurement file.
            run_dir.rename(run_dir.with_name(run_dir.name + f"_failed_{time.time_ns()}"))
        (run_dir / "logs").mkdir(parents=True)
        timing_dir = run_dir / "timing"
        timing_dir.mkdir()
        pitch = modes[item["configuration"]]
        run_env = dict(env, CLO_TIMING_DIR=str(timing_dir), SIMULATOR_PLATFORM=case["preload"],
                       SOFTHIER_BINARY=case["binary"], SOFTHIER_RUN_CWD=str(run_dir / "results/simulator"),
                       SOFTHIER_DRAMSYS_PATH=workload["dramsys_path"] if pitch is not None else str(baseline))
        command = ["taskset", "-c", ",".join(map(str, config["cpus"])),
                   *command_for(pitch, run_dir, config["interval_ps"], config["coupling_mode"],
                                config["server_poll_seconds"], config["adapter_poll_seconds"])]
        record = dict(item, status="running", command=command, loadavg_start=os.getloadavg(),
                      environment={k: run_env[k] for k in sorted(run_env) if k in
                                   ("PYTHON", "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
                                    "SOFTHIER_DRAMSYS_PATH", "SOFTHIER_CORE_MODEL", "SOFTHIER_PRELOAD_MODE")})
        write_json(record_path, record)
        try:
            log = run_dir / "logs" / ("driver.log" if pitch is not None else "simulator.log")
            start, end = execute(command, run_env, log, args.max_host_seconds)
            record.update(start_ns=start, end_ns=end)
            validation = parse_timing((run_dir / "logs/simulator.log").read_text(), case["manifest"],
                                      load_sdk_report(workload["source"]), 16, 1)
            timing = summarize_events(read_events(timing_dir), start, end, pitch is not None, config["interval_ps"])
            record["effective_configuration"] = effective_configuration(run_dir, run_env["SOFTHIER_DRAMSYS_PATH"], pitch is not None)
            diagnostics = run_dir / "state/adapter_diagnostics.json"
            if diagnostics.exists():
                record["adapter_diagnostics"] = json.loads(diagnostics.read_text())
                if record["adapter_diagnostics"]["status"] != "complete":
                    raise ValueError("persistent adapter did not complete successfully")
            record["process_tree_resources"] = json.loads(log.with_suffix(".resources.json").read_text())
            signature = dict(cycles=validation["cycles"], stage_cycles=validation["stage_cycles"],
                             roi_simulated_ps=timing["roi_simulated_ps"])
            if "architectural_signature" in manifest and manifest["architectural_signature"] != signature:
                raise ValueError("architectural cycles changed between configurations")
            if pitch is not None:
                mesh = json.loads((run_dir / "generated/3dice/mesh.json").read_text())
                if mesh["total_cells"] != timing["native_cells"]:
                    raise ValueError("native and generated thermal cell counts disagree")
                record["mesh"] = mesh
            manifest["architectural_signature"] = signature
            record.update(status="complete", validation=validation, timing=timing,
                          output_bytes=sum(p.stat().st_size for p in run_dir.rglob("*") if p.is_file()),
                          loadavg_end=os.getloadavg())
        except BaseException as error:
            record.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                          error=f"{type(error).__name__}: {error}")
            manifest["status"] = record["status"]
            raise
        finally:
            write_json(record_path, record)
            write_json(directory / "study.json", manifest)
    manifest["status"] = "complete"
    write_json(directory / "study.json", manifest)
    analyze(directory)
    print(f"Completed study: {directory}", flush=True)


def main(argv=None):
    args = parse_args(argv)
    if args.command == "analyze":
        print(json.dumps(analyze(args.study), indent=2))
    elif args.command == "smoke":
        from .smoke import run as smoke_run
        smoke_run(args.output, args.replicates, args.coupling_mode)
    else:
        run(args)
