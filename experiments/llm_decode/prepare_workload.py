#!/usr/bin/env python3
"""Stage the SDK decoder, direct-preload ELFs, and functional HBM configuration."""

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SDK = ROOT / "SoftHier/soft_hier_sdk"
MODELS = ("gpt-oss-120b",)


def sha256(path):
    # The common weight ELF is almost 7 GB; never read it all into memory.
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, document):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n")
    temporary.replace(path)


def validate_settings(model, batches, repeats, estimate_scale):
    if not batches or len(set(batches)) != len(batches):
        raise ValueError("decode batches must be a nonempty list without duplicates")
    if any(isinstance(b, bool) or not isinstance(b, int) or not 1 <= b <= model["max_batch"] for b in batches):
        raise ValueError(f"decode batches must be between 1 and {model['max_batch']}")
    if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats <= 0:
        raise ValueError("repeats must be a positive integer")
    if not math.isfinite(estimate_scale) or estimate_scale <= 0:
        raise ValueError("estimate scale must be finite and positive")


def repeat_source(source, repeats):
    """Repeat the complete layer with the same inputs and cache position.

    Every phase recreates its descriptors; caches overwrite the current token's
    position. Repetitions extend the heating history, not the decoded sequence.
    Keep the single-invocation SDK source byte-for-byte unchanged.
    """
    if repeats == 1:
        return source
    start = "    norm(HBM_NORM1_OUT, HBM_INPUT, HBM_NORM1); phase(0);"
    end = "    combine(); phase(13);"
    counter = "ticks[index] = (uint32_t)(now - previous);"
    if any(source.count(anchor) != 1 for anchor in (start, end, counter)):
        raise ValueError("SDK layer boundaries changed; cannot safely apply repetitions")
    source = source.replace(counter, counter.replace(" = ", " += "))
    source = source.replace(start,
        f"    for (unsigned study_repeat = 0; study_repeat < {repeats}; ++study_repeat) {{\n" + start)
    return source.replace(end, end + "\n    }")


def prepare_dramsys(source, destination):
    """Apply the SDK SETUP.md HBM mapping to a fresh private config copy."""
    configs = destination / "dramsys_configs"
    shutil.copytree(source / "dramsys_configs", configs)
    mapping_path = configs / "addressmapping/am_hbm4_emu_16Gb_pc_brc.json"
    mapping = json.loads(mapping_path.read_text())
    if mapping["addressmapping"]["BYTE_BIT"] != [0, 1, 2]:
        raise ValueError("unexpected upstream HBM byte mapping; review SDK SETUP.md")
    for key, bits in mapping["addressmapping"].items():
        mapping["addressmapping"][key] = [bit if bit < 2 else bit - 1 for bit in bits if bit != 2]
    write_json(mapping_path, mapping)
    config_path = configs / "simconfig/example.json"
    config = json.loads(config_path.read_text())
    config["simconfig"].update(StoreMode="Store", DatabaseRecording=False)
    write_json(config_path, config)
    return {str(path.relative_to(destination)): sha256(path)
            for path in sorted(configs.rglob("*.json"))}


def prepare(sdk, llm_model, output, batches, repeats=1, estimate_scale=1.0, log=None):
    sdk, output = Path(sdk).resolve(), Path(output).resolve()
    if llm_model not in MODELS:
        raise ValueError(f"unsupported LLM model: {llm_model}")
    source = sdk / "LLM_decode" / llm_model
    model = json.loads((source / "config/model.json").read_text())
    validate_settings(model, batches, repeats, estimate_scale)
    staged_source = repeat_source((source / "main.c").read_text(), repeats)
    output.mkdir(parents=True, exist_ok=False)
    shutil.copy2(source / "config/model.json", output / "model.json")
    (output / "main.c").write_text(staged_source)
    # The SDK architecture imports its base relative to __file__. Load it at
    # its original location instead of copying it and breaking that relation.
    arch = output / "arch.py"
    arch.write_text('"""Study architecture derived from the pinned SDK decoder."""\n'
        "import runpy\n"
        f"_Base = runpy.run_path({str(source / 'config/arch.py')!r})['FlexClusterArch']\n"
        "class FlexClusterArch(_Base):\n"
        "    def __init__(self):\n"
        "        super().__init__()\n"
        f"        self.power_estimate_scale = {estimate_scale!r}\n"
        "        self.preload_mode = 'direct'\n")
    environment = output / "environment"
    config_hashes = prepare_dramsys(ROOT / "SoftHier/add_dramsyslib_patches", environment)
    subprocess.run([sys.executable, str(source / "tools/generate.py"),
        "--build-dir", str(output), "--model", str(output / "model.json"),
        "--arch", str(arch), "--batches", *map(str, batches)], cwd=output,
        stdout=log, stderr=subprocess.STDOUT if log else None, check=True)
    manifest = {
        "llm_model": llm_model, "decode_batches": batches, "repeats": repeats,
        "source": str(source), "arch": str(arch), "model": model,
        "weights": str(output / "weights.elf"),
        "weights_sha256": sha256(output / "weights.elf"),
        "inputs": {str(batch): {"path": str(output / f"input-b{batch}.elf"),
            "sha256": sha256(output / f"input-b{batch}.elf")} for batch in batches},
        "source_sha256": sha256(source / "main.c"),
        "staged_source_sha256": sha256(output / "main.c"),
        "sdk_commit": subprocess.check_output(["git", "-C", str(sdk), "rev-parse", "HEAD"], text=True).strip(),
        "dramsys_path": str(environment), "dramsys_config_sha256": config_hashes,
        "preload_mode": "direct",
        "scope": "One complete decoder layer using full dimensions and synthetic FP16 weights; not the entire model",
        "repetition_semantics": "Repeat the same layer input and cache position; not autoregressive token generation",
    }
    write_json(output / "workload.json", manifest)
    return manifest


def build(workload, output, log=None):
    source, output = Path(workload["source"]), Path(output)
    env = os.environ.copy()
    sdk = source.parents[1]
    env["PATH"] = str(sdk / "toolchain/install/bin") + os.pathsep + env["PATH"]
    subprocess.run([sys.executable, str(source / "tools/build.py"),
        "--build-dir", str(output), "--arch", workload["arch"],
        "--source", str(output / "main.c")], cwd=output, env=env,
        stdout=log, stderr=subprocess.STDOUT if log else None, check=True)
    workload["binary"] = str(output / "decode.elf")
    workload["binary_sha256"] = sha256(output / "decode.elf")
    write_json(output / "workload.json", workload)
    return workload
