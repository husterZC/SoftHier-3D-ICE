"""Short real simulator check and indicative on/off instrumentation comparison."""
import json
from pathlib import Path
import random
import re
import shutil
import statistics
import sys

from .runner import ROOT, HERE, CONFIGS, PROVIDER, command_for, environment, execute, select_cpus
from .analyze import read_events, summarize_events
from Interface_scripts.providers.softhier.export_system_config import import_architecture


def run(output, replicates=3):
    output = Path(output).resolve()
    output.relative_to(ROOT)
    output.mkdir(parents=True, exist_ok=False)
    sdk = ROOT / "SoftHier/soft_hier_sdk"
    arch = output / "arch.py"
    shutil.copy2(sdk / "apps_dram3d/config/arch.py", arch)
    architecture = import_architecture(arch)
    workload = dict(arch=str(arch), source=str(output), thermal_config=str(CONFIGS / "dram3d_thermal.json"),
                    dram_power_config=str(CONFIGS / "dram3d_power.json"),
                    dramsys_path=str(ROOT / "SoftHier/add_dramsyslib_patches"))
    env = environment(workload)
    execute([sys.executable, str(sdk / "utilities/config.py"), str(arch), "--output-dir", str(output)],
            env, output / "headers.log", 60)
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
    runtime = sdk / "runtime/runtime"
    binary = output / "smoke.elf"
    execute([str(sdk / "toolchain/install/bin/riscv32-unknown-elf-gcc"), "-I", str(output),
             "-isystem", str(runtime / "include"), "-T", str(linker), "-nostartfiles", "-mabi=ilp32d",
             "-march=rv32imafdv_zfh_zvfh", "-mcmodel=medlow", "-O3", "-g", "-Wall", "-Wextra",
             "-fno-builtin", "-fno-tree-vectorize", "-ffp-contract=off", "-ffunction-sections",
             "-fdata-sections", "-Wl,--gc-sections", str(runtime / "flex_start.s"), str(HERE / "smoke.c"),
             "-o", str(binary)], env, output / "build.log", 60)
    rows, signature = [], None
    cpus = ",".join(map(str, select_cpus()))
    rng = random.Random(19)
    # First block warms files; it is kept in raw results but excluded from ratios.
    for block in range(replicates + 1):
        modes = [(pitch, instrument) for pitch in (None, 100.) for instrument in (False, True)]
        rng.shuffle(modes)
        for pitch, instrument in modes:
            mode = "architecture" if pitch is None else "closed_loop_100"
            directory = output / f"b{block}_{mode}_{'on' if instrument else 'off'}"
            (directory / "logs").mkdir(parents=True)
            run_env = dict(env, SOFTHIER_BINARY=str(binary), SIMULATOR_PLATFORM="",
                           SOFTHIER_RUN_CWD=str(directory / "results/simulator"))
            if instrument:
                (directory / "timing").mkdir()
                run_env["CLO_TIMING_DIR"] = str(directory / "timing")
            command = ["taskset", "-c", cpus, *command_for(pitch, directory, 20_000_000)]
            start, end = execute(command, run_env, directory / "logs" /
                                 ("simulator.log" if pitch is None else "driver.log"), 300)
            text = (directory / "logs/simulator.log").read_text()
            if text.count("CLO_SMOKE_PASS sum=49995000") != 1 or "CLO_SMOKE_FAIL" in text:
                raise ValueError(f"smoke numerical check failed: {directory}")
            cycles = re.findall(r"Execution period is (\d+) ns", text)
            if len(cycles) != 1 or (signature is not None and signature != cycles[0]):
                raise ValueError("simulated time changed between matched smoke runs")
            signature = cycles[0]
            row = dict(mode=mode, instrumented=instrument, block=block, pilot=block == 0,
                       host_seconds=(end - start) * 1e-9, simulated_ns=int(signature), path=str(directory))
            if instrument:
                row["timing"] = summarize_events(read_events(directory / "timing"), start, end,
                                                  pitch is not None, 20_000_000)
            config = json.loads((directory / "results/simulator/gvsoc_config.json").read_text())
            row["effective_gvsoc"] = {k: config["target"]["gvsoc"].get(k)
                                      for k in ("power", "debug-mode", "profile-mode")}
            if row["effective_gvsoc"]["power"] != (pitch is not None):
                raise ValueError("unexpected effective power setting")
            rows.append(row)
            (output / "raw.json").write_text(json.dumps(rows, indent=2) + "\n")
    comparison = {}
    for mode in ("architecture", "closed_loop_100"):
        means = {str(enabled): statistics.mean(r["host_seconds"] for r in rows
                 if r["mode"] == mode and r["instrumented"] == enabled and not r["pilot"])
                 for enabled in (False, True)}
        comparison[mode] = dict(mean_seconds=means, observed_on_off_ratio=means["True"] / means["False"])
    result = dict(status="PASS", purpose="short integration and indicative instrumentation check, not B8 results",
                  simulated_ns=int(signature), replicates=replicates, comparison=comparison, runs=rows)
    (output / "validation.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"Short native validation passed: {output}", flush=True)
    return result
