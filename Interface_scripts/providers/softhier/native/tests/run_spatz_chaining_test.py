#!/usr/bin/env python3
"""Check Spatz vector-immediate completion in installed debug/profile builds."""
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

HERE = Path(__file__).resolve().parent
PROVIDER = HERE.parents[1]
REPO = HERE.parents[4]
TARGET = "pulp.chips.soft_hier_old.flex_cluster"
sys.path.insert(0, str(PROVIDER))
from export_system_config import import_architecture
from prepare_dram_config import prepare
from stage_native import build_stamp


def inside(path):
    path = Path(path).resolve()
    path.relative_to(REPO)
    return path


def run(command, cwd, env, log, timeout):
    with log.open("w") as stream:
        process = subprocess.Popen(list(map(str, command)), cwd=cwd, env=env,
            stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise SystemExit(f"Interrupted/timed out; see {log}")
    if code:
        raise SystemExit(f"Command failed ({code}); see {log}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, default=REPO / "SoftHier/.power_interface")
    parser.add_argument("--sdk", type=Path, default=REPO / "SoftHier/soft_hier_sdk")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    sdk, workdir, output = map(inside, (args.sdk, args.workdir, args.output))
    install, dependencies = workdir / "install", workdir / "dependencies"
    systemc = dependencies / "systemc-install"
    build_stamp(REPO / "SoftHier", workdir, TARGET, systemc, "3.0.1", check=True)
    output.mkdir(parents=True, exist_ok=False)
    arch = output / "arch.py"
    shutil.copy2(sdk / "apps_dram3d/config/arch.py", arch)
    architecture = import_architecture(arch)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", SOFTHIER_ARCH_FILE=str(arch),
        SOFTHIER_POWER_PROFILE="constant",
        SOFTHIER_DRAM_POWER_CONFIG=str(PROVIDER / "configs/dram3d_power.json"),
        DRAMSYS_PATH=str(prepare(arch, REPO / "SoftHier/add_dramsyslib_patches", workdir)))
    env["PATH"] = f"{Path(sys.executable).parent}:{install}/bin:" + env.get("PATH", "")
    env["PYTHONPATH"] = f"{install}/python:{sdk}/utilities:" + env.get("PYTHONPATH", "")
    env["LD_LIBRARY_PATH"] = (f"{install}/lib:{systemc}/lib64:"
        f"{dependencies}/dramsys-install:" + env.get("LD_LIBRARY_PATH", ""))
    run([sys.executable, sdk / "utilities/config.py", arch, "--output-dir", output],
        output, env, output / "headers.log", args.timeout)
    # Keep test data below the scratch buffers, using the SDK architecture's
    # instruction-memory placement and the same ELF layout as llm_dram3d.
    linker = output / "memory.ld"
    linker.write_text(f"""OUTPUT_ARCH(riscv)
ENTRY(_start)
MEMORY {{
  L1 (rw) : ORIGIN = {architecture.cluster_tcdm_base + 16:#x}, LENGTH = 0xff0
  CODE (rx) : ORIGIN = {architecture.instruction_mem_base:#x}, LENGTH = {architecture.instruction_mem_size:#x}
}}
PHDRS {{ code PT_LOAD; data PT_LOAD; }}
SECTIONS {{
  .text : {{ *(.init) *(.text*) *(.rodata*) }} > CODE :code
  .data : {{ __global_pointer$ = . + 0x800; *(.sdata*) *(.data*) *(.srodata*) }} > L1 :data
  .bss (NOLOAD) : {{ *(.sbss*) *(.bss*) *(COMMON) }} > L1 :data
  __l1_heap_start = ALIGN(16);
  __hbm_heap_start = 0;
}}
""")
    binary = output / "spatz_chaining.elf"
    runtime = sdk / "runtime/runtime"
    run([sdk / "toolchain/install/bin/riscv32-unknown-elf-gcc", "-I", output,
         "-isystem", runtime / "include", "-T", linker, "-nostartfiles", "-mabi=ilp32d",
         "-march=rv32imafdv_zfh_zvfh", "-mcmodel=medlow", "-O3", "-g", "-Wall", "-Wextra",
         "-fno-builtin", "-fno-tree-vectorize", "-ffp-contract=off", "-ffunction-sections",
         "-fdata-sections", "-Wl,--gc-sections", runtime / "flex_start.s",
         HERE / "spatz_chaining.c", "-o", binary], output, env, output / "build.log", args.timeout)
    run([install / "bin/gvsoc", f"--target={TARGET}", f"--binary={binary}",
         "--core-model=fast", "--preload-mode=direct", "--power", "prepare"],
        output, env, output / "prepare.log", args.timeout)
    base = json.loads((output / "gvsoc_config.json").read_text())
    results = []
    for mode in ("debug", "profile"):
        directory = output / mode
        directory.mkdir()
        config = copy.deepcopy(base)
        gv = config["target"]["gvsoc"]
        gv.pop("power_hook", None)
        gv.update({"debug-mode": mode == "debug", "profile-mode": mode == "profile",
                   "asserts-mode": False, "power": True})
        gv["traces"].update(enabled=False, include_regex=[])
        gv["proxy"]["enabled"] = False
        path = directory / "gvsoc_config.json"
        path.write_text(json.dumps(config) + "\n")
        log = directory / "simulator.log"
        run([install / f"bin/gvsoc_launcher_{mode}_sc", f"--config={path}"],
            directory, env, log, args.timeout)
        expected = "SPATZ_CHAINING_PASS vl=32 checked=32 errors=0"
        text = log.read_text()
        if text.count(expected) != 1 or "SPATZ_CHAINING_FAIL" in text:
            raise SystemExit(f"Missing or failed numerical validation; see {log}")
        results.append(dict(mode=mode, status="PASS", checked=32, errors=0, log=str(log)))
        print(f"{mode}: {expected}", flush=True)
    (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
