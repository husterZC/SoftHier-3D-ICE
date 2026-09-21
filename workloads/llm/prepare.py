#!/usr/bin/env python3
"""Validate and stage synthetic LLM workloads without modifying the pinned SDK."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parents[1]
SDK_REVISION = "1244fdbc34977aff5a6a10ead079053fb5d31d00"
KERNELS = {"norm": "RMSNorm", "acti": "Activation", "gemm": "SummaGEMM", "attn": "FlatAttention"}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_arch(path):
    spec = importlib.util.spec_from_file_location("llm_arch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return vars(module.FlexClusterArch())


def configuration(phase, preset):
    presets = json.loads((PACKAGE / "configs/presets.json").read_text())
    mappings = json.loads((PACKAGE / "configs/mappings_4x4.json").read_text())
    if phase not in mappings or preset not in ("smoke", "original"):
        raise ValueError("expected phase prefill|decode and preset smoke|original")
    config = {**presets["model"], **presets[preset], **mappings[phase], "phase": phase, "preset": preset}
    config["tokens"] = config["prefill_tokens"] if phase == "prefill" else 1
    return config


def validate(c, a):
    """Reject unsupported mappings before header generation or simulation."""
    if (a["num_cluster_x"], a["num_cluster_y"]) != (4, 4):
        raise ValueError("only the validated 4x4 topology is supported; add an explicit mapping for other grids")
    if a["num_core_per_cluster"] != 5 or a["spatz_attaced_core_list"] != [0, 1, 2, 3]:
        raise ValueError("LLM mappings require five cores and four Spatz units per cluster")
    if (a["cluster_tcdm_bank_nb"] != 128 or a["cluster_tcdm_bank_width"] != 32 or a.get("spatz_vlsu_port_width", 32) != 32
            or a["spatz_num_vlsu_port"] != 8 or a["spatz_num_function_unit"] != 4):
        raise ValueError("LLM mapping requires 32-bit TCDM/Spatz ports, eight LSU ports and four function units")
    for key in ("d_model", "d_ff", "n_head", "n_kv_head", "num_layers", "tokens", "max_ctx", "decode_steps"):
        if not isinstance(c[key], int) or c[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    if c["elem_size"] != 2 or c["n_head"] != c["n_kv_head"] or c["d_model"] % c["n_head"]:
        raise ValueError("only FP16 multi-head attention with equal Q/KV head counts is supported")
    if c["n_head"] > 16:
        raise ValueError("TM/HM packing currently requires at most one head per cluster")
    if not 0 <= c["decode_init_cache_len"] <= c["max_ctx"] or c["decode_init_cache_len"] + c["decode_steps"] > c["max_ctx"]:
        raise ValueError("decode would overflow its allocated cache")
    if c["prefill_tokens"] > c["max_ctx"] or c["tokens"] > c["max_ctx"]:
        raise ValueError("prompt exceeds max context")
    d, ff, t, h = c["d_model"], c["d_ff"], c["tokens"], c["d_model"] // c["n_head"]
    g, attn = c["gemm"], c["attention"]
    for mapping in (g, attn):
        if any(not isinstance(v, int) or v <= 0 for v in mapping.values()):
            raise ValueError("group and tile dimensions must be positive integers")
        if 4 % mapping["group_x"] or 4 % mapping["group_y"]:
            raise ValueError("groups must divide the cluster grid")
    if t % (g["group_y"] * g["m_tile"]) or any(n % (g["group_x"] * g["n_tile"]) for n in (d, ff)) or any(k % g["k_tile"] for k in (d, ff)):
        raise ValueError("GEMM dimensions must divide the selected group tiles")
    if attn["block_x"] % attn["group_x"] or attn["block_y"] % attn["group_y"] or t % attn["block_y"]:
        raise ValueError("attention blocks must divide queries and groups")
    lengths = [t] if c["phase"] == "prefill" else range(c["decode_init_cache_len"] + 1, c["decode_init_cache_len"] + c["decode_steps"] + 1)
    if any(n % attn["block_x"] for n in lengths):
        raise ValueError("attention KV lengths must divide the KV block")
    groups = (4 // attn["group_x"]) * (4 // attn["group_y"])
    if groups < c["n_head"] and c["n_head"] % groups:
        raise ValueError("attention would omit remainder heads")
    # The current SUMMA M offset uses group_x. Non-square groups are safe only
    # for the single M iteration used by decode.
    if g["group_x"] != g["group_y"] and t != g["group_y"] * g["m_tile"]:
        raise ValueError("non-square GEMM groups support only a single M iteration")
    br, bc = attn["block_y"] // attn["group_y"], attn["block_x"] // attn["group_x"]
    warm_m = a["redmule_ce_height"]
    warm_n = a["redmule_ce_width"] * (a["redmule_ce_pipe"] + 1)
    warm_k = (a["cluster_tcdm_bank_width"] // 8) * a["cluster_tcdm_bank_nb"] // 2
    warm_bytes = (warm_m * warm_k + warm_k * warm_n + warm_m * warm_n) * 2
    if warm_bytes > a["cluster_zomem_size"]:
        raise ValueError("accelerator initialization exceeds zero-memory window")
    scratch = {
        "accelerator_init": warm_bytes,
        "gemm": 4 * (g["m_tile"] * g["k_tile"] + g["n_tile"] * g["k_tile"] + g["m_tile"] * g["n_tile"]),
        "attention": 2 * (2 * (2 * br * h + 2 * bc * h + br * bc) + 10 * br),
        "pack": min(t, 256) * h * 2,
        "norm_activation_residual": 4 * ff * 2,
    }
    if max(scratch.values()) > a["cluster_tcdm_size"]:
        raise ValueError("kernel scratch exceeds cluster TCDM")
    # Check actual reserved regions, with an independent weight window on the
    # south edge. All casts in the C layout are uint64_t before multiplication.
    base, window = a["hbm_start_base"], a["hbm_node_addr_space"]
    weights = base + window * (2 * a["num_cluster_y"] + a["num_cluster_x"])
    if a["hbm_chan_placement"][0] != 4 or a["hbm_chan_placement"][3] != 4:
        raise ValueError("activation/weight placement requires west and south HBM")
    regions, cursor = [], base
    for name, size in [("hidden", t*d*2), ("norm", t*d*2), ("mlp_mid", t*ff*2),
                       ("mlp_out", t*d*2), ("attention_out", t*d*2), ("qkv_tm", t*d*2),
                       ("q_hm", t*d*2), ("k_hm", t*d*2), ("v_hm", t*d*2), ("o_hm", t*d*2),
                       ("k_cache", c["num_layers"]*c["n_kv_head"]*c["max_ctx"]*h*2),
                       ("v_cache", c["num_layers"]*c["n_kv_head"]*c["max_ctx"]*h*2)]:
        if cursor % 64 or size % 64:
            raise ValueError(f"{name} must be 64-byte aligned")
        regions.append({"name": name, "start": cursor, "size": size, "end": cursor + size})
        cursor += size
    stride = sum((size + 4095) // 4096 * 4096 for size in [d*d*2]*4 + [d*ff*2]*2)
    wsize = stride * c["num_layers"]
    if cursor > base + window or wsize > window or weights % 4096 or cursor > weights:
        raise ValueError("HBM layout exceeds or overlaps mapped windows")
    regions.append({"name": "weights", "start": weights, "size": wsize, "end": weights+wsize})
    return {"regions": regions, "scratch_bytes": scratch, "cache_head_stride_bytes": c["max_ctx"]*h*2, "weight_layer_stride_bytes": stride}


def header(c):
    names = {"tokens": "T", "max_ctx": "MAX_CTX", "d_model": "D_MODEL", "d_ff": "D_FF",
             "n_head": "N_HEAD", "n_kv_head": "N_KV_HEAD", "num_layers": "NUM_LAYERS",
             "elem_size": "ELEM_SIZE", "decode_init_cache_len": "DECODE_INIT_CACHE_LEN", "decode_steps": "DECODE_STEPS"}
    return "#pragma once\n" + "".join(f"#define LLM_{macro} {c[key]}u\n" for key, macro in names.items())


def kernel_configs(c):
    d, t, h = c["d_model"], c["tokens"], c["d_model"] // c["n_head"]
    g, a = c["gemm"], c["attention"]
    return {
        "norm": ("RMSNorm", dict(dtype="fp16", m_size=t, n_size=d)),
        "acti": ("Activation", dict(dtype="fp16", algo="silu", m_size=t, n_size=c["d_ff"], gate_enable=0, bias_enable=0)),
        "gemm": ("SummaGEMM", dict(dtype="fp16", m_size=t, n_size=d, k_size=d,
            m_tile=g["m_tile"], n_tile=g["n_tile"], k_tile=g["k_tile"],
            summa_scale_x=g["group_x"], summa_scale_y=g["group_y"], summa_group_number=1,
            summa_group_reduce=0, summa_group_splitk=0, summa_group_splitn=0,
            summa_group_gap_x=0, summa_group_gap_w=0, summa_group_gap_z=0,
            resha_x_from_enable=0, resha_z_to_enable=0)),
        "attn": ("FlatAttetion", dict(dtype="fp16", kv_sequence_length=t if c["phase"] == "prefill" else c["decode_init_cache_len"]+1,
            q_sequence_length=t, speculative_length=1, head_dimemsion=h, num_head=c["n_head"],
            num_head_group=c["n_kv_head"], batch_size=1, flatten_scale_x=a["group_x"], flatten_scale_y=a["group_y"],
            flatten_shape_x=a["block_x"], flatten_shape_y=a["block_y"], flatten_async=0)),
    }


def prepare(sdk, output, phase="prefill", preset="smoke", arch=None, regression=False):
    sdk, output = Path(sdk).resolve(), Path(output).resolve()
    arch = Path(arch or PACKAGE / "configs/arch_4x4.py").resolve()
    c = configuration(phase, preset)
    if regression:
        if (phase, preset) != ("prefill", "smoke"):
            raise ValueError("kernel regression uses prefill/smoke settings")
        c["tokens"] = c["prefill_tokens"] = 128
    layout = validate(c, load_arch(arch))
    revision = subprocess.check_output(["git", "-C", str(sdk), "rev-parse", "HEAD"], text=True).strip()
    if revision != SDK_REVISION:
        raise ValueError(f"patches require SDK {SDK_REVISION}; found {revision}")
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"staging directory is not empty: {output}; choose a fresh run")
    output.mkdir(parents=True, exist_ok=True)
    sw = output / "sw"
    source_hashes = {}
    for key, name in KERNELS.items():
        source = sdk / "implementation/sw" / name
        shutil.copytree(source, sw / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.elf"))
        for p in source.rglob("*"):
            if p.is_file() and p.suffix in (".c", ".h", ".txt"):
                source_hashes[str(p.relative_to(sdk))] = sha256(p)
    shutil.copytree(PACKAGE / "common", sw / "LLMForwardCommon")
    app = sw / ("LLMForwardPrefill" if phase == "prefill" else "LLMForwardDecode")
    shutil.copytree(PACKAGE / "apps" / phase, app)
    for patch in sorted((PACKAGE / "patches").glob("*.patch")):
        subprocess.run(["patch", "--batch", "--forward", "--fuzz=0", "-p1", "-i", str(patch)], cwd=sw, check=True)
    (app / "include/llm_config.h").write_text(header(c) + ("#define LLM_KERNEL_REGRESSION 1\n" if regression else ""))
    if regression:
        shutil.copyfile(PACKAGE / "tests/kernel_regression.inc", sw / "LLMForwardCommon/src/kernel_regression.inc")
        (app / "main.c").write_text('#include "llm_common.h"\nextern void flex_barrier_xy_init(void);\nextern void flex_eoc(uint32_t);\nint main(void) { flex_barrier_xy_init(); llm_common_kernel_regression(); llm_common_finish(0); return 0; }\n')
    shutil.copyfile(arch, output / "arch.py")
    # Architecture widths are bits; the pinned Spatz LSU consumes bytes and
    # issues each request to a single TCDM bank. Keep VLEN unchanged.
    options = ["**/vu/lsu_width=4", "**/data_noc/vp_component=llm_floonoc"] + [
        f"**/prior_arbiter_{bank}/vp_component=llm_priority_arbiter"
        for bank in range(load_arch(arch)["cluster_tcdm_bank_nb"])]
    (output / "simulator.options").write_text("\n".join(options) + "\n")
    configs = output / "kernel_configs"
    configs.mkdir()
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(sdk / "implementation/scripts")
    for key, (classname, values) in kernel_configs(c).items():
        path = configs / f"{key}.py"
        path.write_text(f"class {classname}:\n    def __init__(self):\n" + "".join(f"        self.{k} = {v!r}\n" for k,v in values.items()))
        subprocess.run([sys.executable, str(sdk / f"implementation/scripts/kernels/{key}_config.py"), str(path),
                        str(sw / KERNELS[key] / "include" / f"{key}.h")], env=env, cwd=output, check=True)
    package_hashes = {str(p.relative_to(PACKAGE)): sha256(p) for p in sorted(PACKAGE.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}
    manifest = {"schema_version": 1, "phase": phase, "preset": preset, "kernel_regression": regression,
        "config": c, "layout": layout, "sdk_revision": revision, "sdk_source_sha256": source_hashes,
        "package_sha256": package_hashes, "architecture_sha256": sha256(output / "arch.py"),
        "app": str(app), "architecture": str(output / "arch.py"),
        "simulator_options": options, "simulator_options_file": str(output / "simulator.options"),
        "scope": "Independent synthetic FP16 benchmark; identity Q/K/V/O and zero MLP weights; no trained weights or text generation."}
    (output / "workload.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("prefill", "decode"), default="prefill")
    parser.add_argument("--preset", choices=("smoke", "original"), default="smoke")
    parser.add_argument("--sdk", type=Path, default=ROOT / "SoftHier/soft_hier_sdk")
    parser.add_argument("--arch", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--regression", action="store_true")
    args = parser.parse_args()
    try:
        result = prepare(args.sdk, args.output, args.phase, args.preset, args.arch, args.regression)
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"LLM preparation failed: {error}\n")
    print(f"Staged {result['phase']}/{result['preset']}: {args.output}")
