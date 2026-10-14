#!/usr/bin/env python3
"""Run the native accounting and DRAMSys ABI tests using repository-local builds."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dependencies", type=Path, required=True)
    parser.add_argument("--dramsys-build", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for path in (args.dependencies, args.dramsys_build, args.output):
        path.resolve().relative_to(REPO)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "tmp").mkdir(exist_ok=True)
    build_env = dict(os.environ, TMPDIR=str(output / "tmp"))
    systemc = args.dependencies.resolve() / "systemc-install"
    library = args.dramsys_build.resolve() / "lib"
    resources = output / "dramsys_configs"
    if not resources.exists():
        shutil.copytree(REPO / "SoftHier/add_dramsyslib_patches/dramsys_configs", resources)
    config = json.loads((resources / "hbm2-example.json").read_text())
    simconfig = json.loads((resources / "simconfig/example.json").read_text())["simconfig"]
    simconfig.update(PowerAnalysis=True, EnableWindowing=False, StoreMode="Store")
    config["simulation"]["simconfig"] = simconfig
    config["simulation"].pop("tracesetup", None)
    config["simulation"]["simulationid"] = "native-energy-test"
    for source in ("hbm2_core_test.cpp", "hbm2_dramsys_test.cpp"):
        binary = output / Path(source).stem
        command = ["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", str(HERE / source), "-o", str(binary)]
        if source == "hbm2_dramsys_test.cpp":
            command += ["-I" + str(systemc / "include"), "-L" + str(library),
                        "-Wl,-rpath," + str(library), "-lDRAMSys_Simulator",
                        "-L" + str(systemc / "lib64"), "-Wl,-rpath," + str(systemc / "lib64"), "-lsystemc", "-pthread"]
        subprocess.run(command, check=True, env=build_env)
        if source == "hbm2_core_test.cpp":
            subprocess.run([str(binary)], cwd=output, check=True)
            continue
        base_mcconfig = json.loads((resources / "mcconfig/fr_fcfs.json").read_text())["mcconfig"]
        for page_policy, refresh_policy, low_power in (("Open", "AllBank", "NoPowerDown"),
                                                       ("Closed", "AllBank", "NoPowerDown"),
                                                       ("Open", "PerBank", "NoPowerDown"),
                                                       ("Open", "AllBank", "Staggered")):
            label = "-".join((page_policy, refresh_policy, low_power))
            print("Native HBM2 scenario: " + label, flush=True)
            config["simulation"]["mcconfig"] = dict(base_mcconfig, PagePolicy=page_policy,
                RefreshPolicy=refresh_policy, PowerDownPolicy=low_power)
            config_path = resources / ("native-energy-test-" + label + ".json")
            config_path.write_text(json.dumps(config, indent=2) + "\n")
            arguments = [str(binary), str(resources), str(config_path),
                         str(HERE.parents[1] / "configs/dram3d_power.json"),
                         "1" if refresh_policy == "PerBank" else "0",
                         "1" if low_power == "Staggered" else "0"]
            subprocess.run(arguments, cwd=output, check=True)


if __name__ == "__main__":
    main()
