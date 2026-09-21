# Synthetic LLM workloads

This package provides independent synthetic FP16 prefill and decode benchmarks
for the 4×4 SoftHier architecture. Workload sources and configuration live in
this repository and use kernels from the pinned SoftHier SDK.

Q/K/V/output projections use identity weights; MLP weights are zero. Hidden
inputs and decode's existing KV cache use deterministic FP16 patterns. The
benchmarks exercise RMSNorm, SUMMA GEMM, FlatAttention, SiLU, residual additions,
and cache DMA. They do not load trained weights, tokenize input, or generate
text. Prefill uses full-sequence attention without a causal mask.

## Run

Complete the root README's bootstrap first. From the repository root:

```bash
make llm-run LLM_PHASE=prefill LLM_PRESET=smoke RUN_NAME=llm_prefill
make llm-run LLM_PHASE=decode LLM_PRESET=smoke RUN_NAME=llm_decode \
  SOFTHIER_POWER_PROFILE=temperature_aware
```

`LLM_PHASE=prefill` and `LLM_PRESET=smoke` are the defaults. Both
`SOFTHIER_POWER_PROFILE=constant|temperature_aware` and
`SOFTHIER_FLOORPLAN=redmule_strip|square_bands` use the existing root interfaces.
Smoke defaults to a 10 µs power interval and a target of 4096 thermal cells.
Original defaults to 100 µs and 65536 cells. Validation uses the provider’s
`SOFTHIER_CORE_MODEL=fast` default. Explicit `POWER_INTERVAL_PS`,
`ICE_TARGET_TOP_DIE_CELLS`, and other normal root thermal controls still apply.
`BUILD_SIMULATOR=0` skips rebuilding native GVSoC, but always builds the newly
staged workload ELF. Use a fresh run directory for every invocation.

| Setting | smoke | original |
|---|---:|---:|
| Transformer layers | 1 | 12 |
| Prefill tokens | 64 | 2048 |
| Model / FF width | 768 / 3072 | 768 / 3072 |
| Q / KV heads | 6 / 6 | 6 / 6 |
| Head width | 128 | 128 |
| Maximum cache context | 128 | 2048 |
| Decode initial cache tokens | 64 | 2047 |
| Decode steps | 2 | 1 |
| Query tokens per decode step | 1 | 1 |

Decode initializes its own cache and appends one token before each attention
call. It does not consume a prefill run's cache. Activation addresses vary with
the phase's working token count; caches are private to a run.

The `original` preset uses 12 transformer layers and 2048 prefill tokens, with
an independently populated decode cache. A full `original` run is a long
experiment; build validation alone does not establish its runtime completion.

## Organization and mappings

- `common/`: shared layer execution, initialization, memory layout, cache helpers.
- `apps/prefill/`, `apps/decode/`: phase entry points.
- `configs/presets.json`: model sizes and phase lengths.
- `configs/mappings_4x4.json`: explicit validated kernel mappings.
- `configs/arch_4x4.py`: 4×4 architecture (16 clusters,
  five scalar cores and four Spatz units per cluster, 384 KiB TCDM, 1024-bit NoC).
- `patches/`: small, versioned corrections applied only to staged SDK kernels.
- `simulator/`, `build_model.py`: run-local native compatibility models.
- `prepare.py`, `run.py`, `validate_run.py`: preparation, execution, acceptance.
- `tests/`: host checks and an on-device numerical kernel regression.

Prefill uses one 4×4 GEMM group with M/N/K tiles 16/64/64 and synchronous
attention in a 4×4 group with 64×64 blocks. Decode uses one 4×1 GEMM group with
1/64/64 tiles and synchronous 1×1 attention groups with 1×1 blocks. Six decode
attention groups handle one head each; other groups wait at the global barriers.

The package deliberately supports only 4×4 with four Spatz units. Changing the
grid requires a new explicit mapping and simulator validation. Preparation
rejects unsupported topology, invalid tiling, omitted attention heads, cache
overflow, insufficient TCDM, misalignment, and HBM window overflow/overlap.
Each accelerator first executes one full physical tile of zeros. The pinned
LightRedmule model leaves padding buffers uninitialized before its first compute;
this warm-up makes small logical tiles deterministic without editing the model.
Initialization is included in the measured power trace.

Run-local `simulator.options` sets `**/vu/lsu_width=4`. The pinned target passes
its 32-bit Spatz port width to an LSU property measured in bytes; four-byte
requests are required by the TCDM banks. This override preserves the architecture
and 256-bit vector registers and is recorded in the workload manifest.

The one-token attention tile also requires two-byte DMA and accelerator
accesses. The pinned bank arbiter incorrectly requires exactly four bytes.
`build_model.py` patches a copy to accept accesses contained within a bank word,
retaining the original arbitration latency. It builds a separate shared model
using the native CMake compiler/ABI flags and selects it through run-local
configuration. The original libraries and source files remain unchanged.
This requires the provider's native CMake build tree (created by bootstrap or
`--build-hardware`). Model sources, library hashes and simulator revisions are
recorded alongside the ELF.

A second run-local model fix initializes each NoC reduction from its first
response. The pinned implementation adds responses to reused DMA-buffer data,
which corrupts attention after GEMM traffic. The native patches are described
in [simulator/README.md](simulator/README.md). A small engine correction also
clears negative dynamic-power roundoff below one picowatt when a component
becomes idle. This prevents false failures of the cumulative-energy check.
Run-local engine libraries retain the original ABI; the installed engine is
unchanged.

Activations use `ARCH_HBM_START_BASE`; weights use the first south-edge window,
computed from the generated architecture's grid dimensions and HBM window size.

## Reproducibility and outputs

Each run lives in `runs/<RUN_NAME>/<timestamp>/`:

```text
generated/llm/
  arch.py, simulator.options, kernel_configs/, workload.json
  sw/                         copied LLM sources and patched SDK kernels
  runtime/sdk_snapshot/       generated runtime headers and linker inputs
build/llm/                    CMake build, ELF, disassembly
artifacts/llm/                archived ELF, effective simulator JSON, native models
logs/workload-build.log, logs/model-build.log
logs/simulator.log
traces/, results/3dice/       standard power and thermal outputs
run.env, summary.txt         standard coupled-run records
```

`workload.json` records the exact settings, memory plan, SDK revision, source
and patch hashes, generated source/header hashes, ELF hash, status, and acceptance
results. Failed runs retain their logs and failure status. SDK and simulator
sources are not edited. Native GVSoC installations and DRAMSys working files are
shared by the existing provider, so run these jobs sequentially.

## Validation commands

See [VALIDATION.md](VALIDATION.md) for the recorded checks and their scope.

```bash
make llm-tests
make interface-tests
python3 -m unittest discover -s experiments/component_power -p 'test_*.py'

# Build without launching the simulator (either phase/preset).
python3 workloads/llm/run.py --mode build --phase decode --preset original

# Standalone smoke; add --build-hardware if native GVSoC needs rebuilding.
python3 workloads/llm/run.py --mode standalone --phase prefill --preset smoke
python3 workloads/llm/run.py --mode standalone --phase decode --preset smoke

# Real kernels: identity GEMM over multiple M/N/K tiles, six attention heads,
# 64-row prefill and one-row decode with live KV length below allocated capacity.
python3 workloads/llm/run.py --mode standalone --regression
```

All ordinary phase runs check every final hidden element for finite FP16 values
and write an explicit application exit marker. The pinned simulator discards
the EOC value and returns zero even for application failures, so the runner
requires both success markers and returns nonzero if either is absent. This finite check
is not an end-to-end model accuracy test. The dedicated regression compares
actual kernel outputs with known identity/constant-value results.

Coupled acceptance additionally checks simulator/server exit codes,
init/update/final hook phases, contiguous windows, exact component coverage,
finite power/temperature values, and the final termination sentinel. To repeat
those checks on an existing run:

```bash
python3 workloads/llm/validate_run.py runs/llm_prefill/<timestamp>
```
