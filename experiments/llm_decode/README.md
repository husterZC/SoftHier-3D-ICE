# LLM decoder-layer thermal co-simulation

Run the SDK's `LLM_decode` applications through the existing SoftHier provider,
3D-ICE hook, selectable floorplans, and temperature-map renderer. As in
[`component_power`](../component_power/README.md), each case runs both `constant`
and `temperature_aware` leakage with identical binaries, preloads, and geometry.

The currently supported model is **`gpt-oss-120b`**: one complete decoder layer
with full dimensions, synthetic FP16 parameters, top-4 expert routing, and a
2,048-token KV cache. This is not the entire multi-layer model or a trained
checkpoint. See the pinned [SDK application guide](../../SoftHier/soft_hier_sdk/LLM_decode/gpt-oss-120b/README.md)
for its numerical computation and mapping.

## Fresh clone and environment

First install the Linux host packages and Conda described in the
[root setup guide](../../README.md#os-requirements). The commands below use
Python 3.12 and keep the simulator, SDK, native dependencies, and generated
artifacts inside the checkout. GitHub SSH access is required by the 3D-ICE
submodule URL; the SDK and native dependency downloads also require network
access.

```bash
git clone --branch chi/map --recurse-submodules \
  https://github.com/husterZC/SoftHier-3D-ICE.git
cd SoftHier-3D-ICE

conda create -y -n py312 python=3.12 pip
conda activate py312

python -m pip install --upgrade pip setuptools wheel
python -m pip install \
  -r SoftHier/requirements.txt \
  -r SoftHier/core/requirements.txt \
  -r SoftHier/gapy/requirements.txt \
  -r SoftHier/gvrun/requirements.txt \
  cmake==3.31.10 numpy matplotlib Pillow shapely rtree
python -m pip check
```

These Python packages cover this experiment's generation, numerical reference,
geometry, plotting, and GIF rendering. The optional GDS tooling in
`3D-ICE/requirements.txt` is not needed for this workflow.

In each new shell, activate the environment and select its build tools before
running the commands below:

```bash
conda activate py312
export PYTHON="$(command -v python)"
export CMAKE="$(command -v cmake)"
export SOFTHIER_DRAMSYS_CMAKE="$CMAKE"
export SOFTHIER_SDK_URL=https://github.com/pulp-platform/softhier-sdk.git
export SOFTHIER_BOOTSTRAP_JOBS=8
export CMAKE_FLAGS='-j 8'

mkdir -p tmp/llm_decode/tmp
export TMPDIR="$PWD/tmp/llm_decode/tmp"
```

Adjust the two build-job settings for the host's available CPUs and memory.
Bootstrap selects the pinned SDK revision and installs its RISC-V toolchain,
SystemC, patched DRAMSys, and 3D-ICE; no separate SDK checkout or manual HBM
configuration is required. The runner prepares the private HBM configuration.
Large preloads and archived copies require several tens of GB of disk space;
the weight ELF alone is approximately 6.9 GB.

## Run the requested batch comparison

Run from the repository root after the environment setup above:

```bash
make bootstrap

python experiments/llm_decode/run_study.py \
  --prefix gptoss120b_square_b1_b8 \
  --llm_model gpt-oss-120b --decode_batch 1 8 \
  --floorplan square_bands --repeats 1 --build-hardware

python experiments/llm_decode/analyze.py \
  --manifest experiments/llm_decode/logs/gptoss120b_square_b1_b8/study.json \
  --output experiments/llm_decode/results/gptoss120b_square_b1_b8 \
  --animations --full-maps
```

This produces four co-simulations and eight GIFs: component-average and thermal-cell
animations for both profiles at both batches. Omit `--full-maps` for just the four
component-average GIFs. A single case can use `--decode_batch 1` or `--decode_batch 8`.
Use a new prefix for another study; existing artifacts are not overwritten and
the runner has no resume mode. Run studies sequentially because native builds
and SDK toolchain setup are shared.

Omit `--build-hardware` after a compatible simulator is built. This option builds
native models, while the SDK's own `tools/build.py` always builds the decoder.
It does not build 3D-ICE; `make bootstrap` supplies the thermal solver.

## Settings

| Option | Default | Meaning |
|---|---|---|
| `--llm_model` | `gpt-oss-120b` | SDK LLM application. `--llm-model` is an alias. |
| `--decode_batch` | `1` | One or more unique batch sizes from 1 through 64. `--decode-batch` is an alias. |
| `--repeats` | `1` | Repeat the complete layer execution region with the same input and cache position. |
| `--interval-ps` | `20000000` | One power/temperature interval for the whole layer: **20 µs**. |
| `--cells` | `16384` | Approximate top-die thermal cell target. |
| `--floorplan` | `redmule_strip` | Original placement or `square_bands`. |
| `--estimate-scale` | `1.0` | New-logic power coefficient multiplier, as in component-power studies. |
| `--prefix` | `decode` | New study identifier for logs, workloads, and raw runs. |
| `--build-hardware` | Off | Build native GVSoC models before running the decoder. |

There is no attention-specific interval: all 14 stages use `--interval-ps`.
The thermal integration step is one tenth of that interval. Repeats accumulate
stage counters and extend the thermal history; they do not advance the token
position or perform autoregressive generation. With one repeat the staged
application source is unchanged from the SDK.

## Files and responsibilities

| File | Responsibility |
|---|---|
| `run_study.py` | Prepare shared inputs, build, run both profiles, validate timing/numerics, and archive provenance. |
| `prepare_workload.py` | Stage architecture/source, invoke the SDK generator/build tools, and prepare a private HBM configuration. |
| `analyze.py` | Validate pairs and reuse the component-power analysis and thermal animation functions. |
| `test_llm_decode.py` | Regression tests for staging, settings, timing, and manifest validation. |

Generated data is Git-ignored:

```text
experiments/llm_decode/
  logs/<prefix>/                 study.json, frozen provider, build/run/reference logs
  workloads/<prefix>/             one decoder ELF, common weights, per-batch input ELFs
  results/<name>/                 report.md, PNG/PDF plots, CSVs, GIFs
runs/llm_decode/<prefix>_<model>_b<batch>_<profile>/
  generated/                     system contract, floorplan, stack, hook config
  traces/                        power exchanges and component temperatures
  results/3dice/                 thermal-cell and floorplan-average temperatures
  results/simulator/             dump_0, timing.json, validation.json, reference.npz
  artifacts/                     executable, both preloads, SDK manifest, source and configs
```

Open `results/<name>/report.md` first. `metrics.csv` distinguishes layer latency
from total simulated thermal duration and distinguishes component-average peaks
from thermal-cell peaks. `stages.csv` contains the 14-stage timing breakdown.
`validation.csv` contains power/leakage residuals. Other outputs include
`component_energy.csv`, `timeseries.csv`, a representative cluster floorplan,
and the same power/temperature comparison plots used by `component_power`.

## Untimed preload and reproducibility

The parent repo selects SoftHier commit `0ab0442c07d030d380010c59d3004d5a5b7c7f3f`.
The provider pins SDK branch `chi/soft_hier_old_llm_map` at
`16b52e5244be9c6695e9069096bcc871d716b963`.

The common weight preload is approximately 6.9 GB. Generate it once per study;
all batches and profiles share it. The provider uses `--preload-mode=direct`,
passes weights with `--preload`, and adds the batch ELF through
`**/hbm_preloader/binary`. Every loader must report completion at cycle zero.
Artifact copies use filesystem copy-on-write where available; allow sufficient
disk space for independent copies on other filesystems. Hashing streams large
ELFs instead of loading them into Python memory.

The experiment copies the tracked DRAMSys configuration before applying the
SDK's documented 32-bit pseudo-channel address mapping, enables functional
storage, and disables database recording. It does not modify tracked HBM
configs or SDK runtime headers. The run manifest records the actual compiler
outputs, SDK revision, input hashes, private DRAMSys configuration hashes, and
the normal provider provenance.

For an already activated Python virtual environment instead of Conda, use
`export SOFTHIER_CONDA_ENV=''`. The project-local environment prepared for the
initial evaluation can be selected with `source tmp/llm_decode/env.sh`; that
machine-specific file is ignored and is not present in a fresh clone. Use the
setup commands above on another installation.

## Validation and interpretation

Every run requires successful simulator and 3D-ICE exits, `DECODE_DONE`, 14
stage counters summing to `LAYER_CYCLES`, valid routing counts, and zero-cycle
ELF preload. The SDK NumPy reference checks exact expert IDs and finite FP16
results with 0.01 absolute and 0.02 relative tolerances. Analysis also requires
matching paired binaries/inputs, numerical dumps, geometry, stage timings,
power windows, dynamic power, and temperature-dependent leakage coefficients.

The reference single-layer latencies supplied for batches 1 and 8 are about
3.432185 ms and 11.745299 ms. Compare these with `layer_ms`, not the full thermal
duration, which also contains startup, synchronization, printing, and dumping.
Power is integrated over exact simulator windows; the final partial thermal
window still advances a full slot, following the existing co-simulation convention.

Synthetic routing, estimated component power/areas, excluded HBM/PHY power,
and a cold start limit interpretation. These runs establish transient thermal
behavior for this mapping, not calibrated silicon power or steady-state temperature.

Maintainer checks:

```bash
make interface-tests
python -m unittest discover -s experiments/llm_decode -p 'test_*.py' -v
python -m unittest discover -s experiments/component_power -p test_analysis.py -v
```
