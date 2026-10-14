#!/usr/bin/env python3
"""Stage the pinned stacked-DRAM SDK application and explicit model inputs."""

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SDK = ROOT / "SoftHier/soft_hier_sdk"
SDK_COMMIT = "b454aeee4278bb00ecbfcdcc4d1366fe8451d7c8"
CONFIGS = ROOT / "Interface_scripts/providers/softhier/configs"
CASES = tuple(f"decode_b{batch}_kv{past}" for batch in (1, 8, 16)
              for past in (1024, 2048, 4096)) + ("prefill_s64", "prefill_s128")
MODEL = "gpt-oss-120b"
sys.path.insert(0, str(ROOT))


def within_repo(path):
    path = Path(path).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError(f"path must remain inside this repository: {path}")
    return path


def sha256(path):
    digest = hashlib.sha256()
    with within_repo(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, document):
    path = within_repo(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n")
    temporary.replace(path)


def source_hashes(folder):
    return {str(path.relative_to(folder)): sha256(path)
            for path in sorted(folder.rglob("*")) if path.is_file()
            and "__pycache__" not in path.parts and path.suffix != ".pyc"}


def native_source_hashes(provider=None):
    """Hash the native backend and the provider helpers that stage/build it."""
    provider = within_repo(provider or CONFIGS.parent)
    hashes = source_hashes(provider / "native")
    hashes.update({"provider_helpers/" + path.name: sha256(path)
                   for path in sorted(provider.glob("*.py"))})
    return hashes


def snapshot_native_sources(destination, provider=None):
    provider = within_repo(provider or CONFIGS.parent)
    destination = within_repo(destination)
    shutil.copytree(provider / "native", destination,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    helpers = destination / "provider_helpers"
    helpers.mkdir()
    for path in sorted(provider.glob("*.py")):
        shutil.copy2(path, helpers / path.name)
    hashes = source_hashes(destination)
    if hashes != native_source_hashes(provider):
        raise ValueError("native/provider sources changed while being archived")
    return hashes


def validate_repeats(repeats):
    if isinstance(repeats, bool) or not isinstance(repeats, int) or not 1 <= repeats <= 0xffffffff:
        raise ValueError("repeats must be a positive 32-bit integer")


def validate_settings(cases, layer, seed, estimate_scale, repeats=1):
    validate_repeats(repeats)
    if not cases or len(set(cases)) != len(cases) or any(case not in CASES for case in cases):
        raise ValueError("cases must be supported SDK presets without duplicates")
    if isinstance(layer, bool) or not isinstance(layer, int) or not 0 <= layer < 36:
        raise ValueError("layer must be an integer between 0 and 35")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    if not math.isfinite(estimate_scale) or estimate_scale <= 0:
        raise ValueError("estimate scale must be finite and positive")


def repeat_source(source, repeats):
    """Repeat the staged layer with the same inputs and cache position.

    The SDK overwrites intermediate results and the current token's K/V slots;
    accumulators are cleared by each operation. Validate the final iteration
    once, outside the timed region. Leave the one-repeat SDK source unchanged.
    """
    validate_repeats(repeats)
    if repeats == 1:
        return source
    start = "    STAGE(0, norm(cid, L_OFF_X, L_OFF_NORM1, L_OFF_NORM1_WEIGHT));"
    end = "    STAGE(10, combine(cid));"
    declaration = "    unsigned previous = 0, stages[11];"
    counter = "unsigned now = cycles(); stages[id] = now - previous; \\\n        elapsed += stages[id]; previous = now;"
    output = '            printf("LLM_STAGE id=%u cycles=%u\\n", i, stages[i]);'
    anchors = (start, end, declaration, counter, output)
    if any(source.count(anchor) != 1 for anchor in anchors) or source.index(start) >= source.index(end):
        raise ValueError("SDK layer boundaries changed; cannot safely apply repetitions")
    source = source.replace(declaration,
        "    unsigned previous = 0, study_repeat;\n    uint64_t stages[11] = {0};")
    source = source.replace(counter,
        "unsigned now = cycles(), delta = now - previous; \\\n        stages[id] += delta; elapsed += delta; previous = now;")
    first, last = source.index(start), source.index(end) + len(end)
    body = "\n".join("    " + line for line in source[first:last].splitlines())
    source = (source[:first] +
        f"    for (study_repeat = 0; study_repeat < {repeats}u; ++study_repeat) {{\n" +
        body + "\n    }" + source[last:])
    # flex_printf disables long-long formatting. Split 64-bit accumulators into
    # words; the runner normalizes them before invoking the unchanged SDK report.
    return source.replace(output,
        '            printf("LLM_STAGE id=%u cycles_hi=%u cycles_lo=%u\\n", i,\n'
        '                   (unsigned)(stages[i] >> 32), (unsigned)stages[i]);\n'
        '        printf("LLM_DRAM3D_REPEATS count=%u\\n", study_repeat);')


def validate_inputs(thermal_config, dram_power_config):
    # Keep validation authoritative in the provider, before generating GBs of data.
    from Interface_scripts.providers.softhier.thermal_config import load_thermal_config
    from Interface_scripts.providers.softhier.native.power_config import load_power_config
    return (load_thermal_config(within_repo(thermal_config)),
            load_power_config(within_repo(dram_power_config)))


def prepare_dramsys(source, destination):
    source, destination = within_repo(source), within_repo(destination)
    configs = destination / "dramsys_configs"
    shutil.copytree(source / "dramsys_configs", configs)
    channel_path = configs / "hbm2-example.json"
    channel = json.loads(channel_path.read_text())
    simulation = channel["simulation"]
    mapping = within_repo(configs / simulation["addressmapping"])
    original_mapping = sha256(mapping)
    original_simconfig = within_repo(configs / simulation["simconfig"])
    config_path = configs / "simconfig/softhier_dram3d_power.json"
    config = json.loads(original_simconfig.read_text())
    config["simconfig"].update(StoreMode="Store", DatabaseRecording=False,
                             PowerAnalysis=True, EnableWindowing=False)
    write_json(config_path, config)
    simulation["simconfig"] = "simconfig/softhier_dram3d_power.json"
    write_json(channel_path, channel)
    if sha256(mapping) != original_mapping:
        raise ValueError("native HBM2 address mapping changed during staging")
    return source_hashes(destination)


def load_sdk_report(source):
    path = within_repo(Path(source) / "scripts/run.py")
    spec = importlib.util.spec_from_file_location("dram3d_sdk_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.report


def stage_architecture(source, destination, estimate_scale):
    # SDK utilities/config.py textually turns self.FIELD assignments into C/ASM
    # constants. Simulator-only floats must not become invalid assembly .equ's.
    destination.write_text(source.read_text() + "\n_OriginalArchitecture = FlexClusterArch\n"
        "class FlexClusterArch(_OriginalArchitecture):\n"
        "    def __init__(self):\n        super().__init__()\n"
        f"        setattr(self, 'power_estimate_scale', {estimate_scale!r})\n"
        "        setattr(self, 'preload_mode', 'direct')\n")


def prepare(sdk, output, cases, thermal_config=None, dram_power_config=None,
            layer=1, seed=19, estimate_scale=1.0, all_experts=False,
            plan_only=False, dramsys_source=None, log=None, repeats=1):
    sdk, output = within_repo(sdk), within_repo(output)
    validate_settings(cases, layer, seed, estimate_scale, repeats)
    thermal_config = thermal_config or CONFIGS / "dram3d_thermal.json"
    dram_power_config = dram_power_config or CONFIGS / "dram3d_power.json"
    resolved_inputs = validate_inputs(thermal_config, dram_power_config)
    source = within_repo(sdk / "llm_dram3d" / MODEL)
    upstream_arch = within_repo(sdk / "apps_dram3d/config/arch.py")
    commit = subprocess.check_output(["git", "-C", str(sdk), "rev-parse", "HEAD"], text=True).strip()
    if commit != SDK_COMMIT:
        raise ValueError(f"expected SDK {SDK_COMMIT}, found {commit}")
    staged_main = repeat_source((source / "sw/main.c").read_text(), repeats)
    output.mkdir(parents=True, exist_ok=False)
    staged = output / "source"
    staged.mkdir()
    for folder in ("scripts", "sw"):
        shutil.copytree(source / folder, staged / folder,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(source / "Makefile", staged / "Makefile")
    (staged / "sw/main.c").write_text(staged_main)
    native_hashes = snapshot_native_sources(output / "native_source")
    # This SDK architecture is self contained. Preserve its source and append
    # the study overrides, so archives remain usable after moving the SDK.
    arch = output / "arch.py"
    stage_architecture(upstream_arch, arch, estimate_scale)
    workload = dict(experiment="llm_dram3d", version=1, llm_model=MODEL,
        sdk=str(sdk), sdk_commit=commit, source=str(staged), original_source=str(source),
        source_sha256=source_hashes(staged), native_source_sha256=native_hashes,
        arch=str(arch), arch_sha256=sha256(arch),
        upstream_arch_sha256=sha256(upstream_arch), cases={}, layer=layer, seed=seed,
        all_experts=all_experts, estimate_scale=estimate_scale, plan_only=plan_only, repeats=repeats,
        preload_mode="direct", dram_power_temperature_dependence=False,
        scope="One complete transformer layer with deterministic synthetic weights")
    if resolved_inputs:
        for name, original, normalized in zip(("thermal", "dram_power"),
                (thermal_config, dram_power_config), resolved_inputs):
            destination = output / f"{name}_config.json"
            shutil.copy2(within_repo(original), destination)
            workload[f"{name}_config"] = str(destination)
            workload[f"{name}_config_sha256"] = sha256(destination)
            workload[f"resolved_{name}_config"] = normalized
    if not plan_only:
        environment = output / "environment"
        workload["dramsys_config_sha256"] = prepare_dramsys(
            dramsys_source or ROOT / "SoftHier/add_dramsyslib_patches", environment)
        workload["dramsys_path"] = str(environment)
    env = dict(os.environ, OPENBLAS_NUM_THREADS="1", PYTHONDONTWRITEBYTECODE="1")
    for case in cases:
        destination = output / case
        destination.mkdir()
        command = [sys.executable, str(staged / "scripts/generate.py"), "--arch", str(arch),
            "--case", case, "--layer", str(layer), "--seed", str(seed), "--output", str(destination)]
        if all_experts:
            command.append("--all-experts")
        if plan_only:
            plan = json.loads(subprocess.check_output(command + ["--plan-only"], cwd=output, env=env, text=True))
            write_json(destination / "plan.json", plan)
            workload["cases"][case] = dict(output=str(destination), plan=plan)
        else:
            subprocess.run([sys.executable, str(sdk / "utilities/config.py"), str(arch),
                "--output-dir", str(destination)], cwd=output, env=env, stdout=log,
                stderr=subprocess.STDOUT if log else None, check=True)
            subprocess.run(command, cwd=output, env=env, stdout=log,
                stderr=subprocess.STDOUT if log else None, check=True)
            workload["cases"][case] = dict(output=str(destination),
                manifest=json.loads((destination / "manifest.json").read_text()),
                preload=str(destination / "preload.elf"),
                preload_sha256=sha256(destination / "preload.elf"))
    write_json(output / "workload.json", workload)
    return workload


def build(workload, log=None):
    if workload["plan_only"]:
        raise ValueError("a plan-only workload cannot be compiled")
    sdk, source = within_repo(workload["sdk"]), within_repo(workload["source"])
    for case in workload["cases"].values():
        output = within_repo(case["output"])
        subprocess.run(["make", "-f", str(source / "Makefile"), "compile",
            f"SDK_DIR={sdk}", f"RUNTIME={sdk / 'runtime/runtime'}", f"OUT={output}"],
            cwd=output, stdout=log, stderr=subprocess.STDOUT if log else None, check=True)
        case.update(binary=str(output / "layer.elf"), binary_sha256=sha256(output / "layer.elf"))
    write_json(source.parent / "workload.json", workload)
    return workload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, default=SDK)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", choices=CASES, nargs="+", default=[CASES[0]])
    parser.add_argument("--thermal-config", type=Path)
    parser.add_argument("--dram-power-config", type=Path)
    parser.add_argument("--layer", type=int, default=1)
    parser.add_argument("--seed", type=int, default=19)
    parser.add_argument("--repeats", type=int, default=1,
                        help="repeat the complete layer with the same inputs and KV-cache position")
    parser.add_argument("--estimate-scale", type=float, default=1.0)
    parser.add_argument("--all-experts", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args(argv)
    workload = prepare(**vars(args))
    print(f"Prepared {len(workload['cases'])} case(s): {args.output / 'workload.json'}")


if __name__ == "__main__":
    main()
