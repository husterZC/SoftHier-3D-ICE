#!/usr/bin/env python3
"""Prepare an SDK implementation app, kernel headers and preload outside the SDK.

Called automatically by run_study.py. Use directly for a custom architecture
or kernel configuration; this step does not compile the app or run a simulator.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from Interface_scripts.providers.softhier.prepare_workload import copy_tree

KERNELS = {"norm": "RMSNorm", "acti": "Activation", "gemm": "SummaGEMM", "attn": "FlatAttention"}


def prepare(sdk, kernel, output, arch, config, repeats=1):
    if repeats < 1:
        raise ValueError("repeats must be positive")
    implementation = sdk / "implementation"
    app = output / "app"
    copy_tree(implementation / "sw" / KERNELS[kernel], app)
    if repeats > 1:
        # Repeat the SDK execution region, including its descriptor setup.
        # Reusing only Run(&info) would skip later RMSNorm/Activation work,
        # since those functions advance info.start_token to the end.
        source = (app / "main.c").read_text()
        pattern = r"(/\*\*+\*/\s*/\*  Program Execution Region -- Start \*/\s*/\*\*+\*/)(.*?)(/\*\*+\*/\s*/\*  Program Execution Region -- Stop  \*/\s*/\*\*+\*/)"
        source, count = re.subn(pattern, lambda m: m[1] +
            f"\n    for (uint32_t study_repeat = 0; study_repeat < {repeats}; ++study_repeat) {{\n" +
            m[2] + "\n        flex_global_barrier_xy();\n    }\n    " + m[3], source, flags=re.S)
        if count != 1:
            raise ValueError("SDK execution-region markers changed; repetition was not applied")
        (app / "main.c").write_text(source)
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(sdk / "utilities"),
        str(implementation / "scripts"), env.get("PYTHONPATH", "")])
    env["PATH"] = str(sdk / "toolchain/install/bin") + os.pathsep + env["PATH"]
    scripts = implementation / "scripts/kernels"
    subprocess.run([sys.executable, str(scripts / f"{kernel}_config.py"), str(config),
                    str(app / "include" / f"{kernel}.h")], env=env, cwd=output, check=True)
    subprocess.run([sys.executable, str(scripts / f"{kernel}_preload.py"),
                    str(app / "preload.elf"), str(app / "include/preload.h"),
                    str(arch), str(config)], env=env, cwd=output, check=True)
    manifest = {"kernel": kernel, "repeats": repeats, "source": str(implementation / "sw" / KERNELS[kernel]),
                "app": str(app), "arch": str(arch), "kernel_config": str(config),
                "preload": str(app / "preload.elf"),
                "preload_sha256": hashlib.sha256((app / "preload.elf").read_bytes()).hexdigest(),
                "validation": "SDK timing-workload preload; not an independent numerical golden test"}
    (output / "workload.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kernel", choices=KERNELS,
        help="SDK app: norm=RMSNorm, acti=SiLU, gemm=SUMMA GEMM, attn=FlatAttention")
    parser.add_argument("--output", type=Path, required=True,
        help="staging directory for app/ and workload.json; choose a fresh path to avoid overlaying files")
    parser.add_argument("--sdk", type=Path, default=ROOT / "SoftHier/soft_hier_sdk",
        help="SDK checkout directory (default: %(default)s)")
    parser.add_argument("--arch", type=Path,
        help="architecture used for preload generation (default: <sdk>/implementation/config/arch/arch.py)")
    parser.add_argument("--kernel-config", type=Path,
        help="kernel parameter file (default: <sdk>/implementation/config/kernels/<kernel>.py)")
    parser.add_argument("--repeats", type=int, default=1,
        help="positive execution-region repetitions in the staged source (default: %(default)s)")
    args = parser.parse_args()
    sdk = args.sdk.resolve()
    arch = (args.arch or sdk / "implementation/config/arch/arch.py").resolve()
    config = (args.kernel_config or sdk / f"implementation/config/kernels/{args.kernel}.py").resolve()
    print(json.dumps(prepare(sdk, args.kernel, args.output.resolve(), arch, config, args.repeats), indent=2))
