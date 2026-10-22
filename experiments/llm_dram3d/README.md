# LLM layer thermal co-simulation on memory-on-logic SoftHier

Run the SDK's `llm_dram3d` application through the existing SoftHier provider,
3D-ICE hook, selectable floorplans, and temperature-map renderer. As in
[`llm_decode`](../llm_decode/README.md), each case runs both `constant` and
`temperature_aware` logic leakage with identical binaries, preloads, and geometry.

The supported model is **`gpt-oss-120b`**: one complete transformer layer with
full dimensions, deterministic synthetic FP16 parameters, and top-4 expert
routing. One face-to-face DRAM die has 16 channel regions, each covering exactly
its corresponding cluster on the 4×4 logic die. DRAMPower supplies DRAM core
command, refresh, and background energy; controller and PHY power are excluded.
See the pinned [SDK application guide](../../SoftHier/soft_hier_sdk/llm_dram3d/gpt-oss-120b/README.md)
for the numerical computation and mapping.

## Fresh clone and environment

First install the Linux host packages and Conda described in the
[root setup guide](../../README.md#os-requirements). The commands below use
Python 3.12 and keep the simulator, SDK, native dependencies, and generated
artifacts inside the checkout. Use a checkout containing this experiment and
its memory-on-logic provider changes. GitHub SSH access is required by the
3D-ICE submodule URL; SDK and native dependency downloads also require network
access.

```bash
git clone --branch chi/softhier3d --recurse-submodules \
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

These Python packages cover workload generation, numerical reference, geometry,
plotting, and GIF rendering. The optional GDS tooling in
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

mkdir -p tmp/llm_dram3d/tmp
export TMPDIR="$PWD/tmp/llm_dram3d/tmp"
```

Adjust the build-job settings for the host's CPUs and memory. Bootstrap selects
the pinned SDK revision and installs its RISC-V toolchain, SystemC, patched
DRAMSys, and 3D-ICE. The runner prepares the private HBM2 configuration. Preload
size depends on the case and active experts: B1/KV1024 uses about 260.5 MB;
larger cases or `--all-experts` can require several GB plus archived copies.
All experiment input/output paths must remain inside this repository.

## Run the requested case comparison

Run from the repository root after the environment setup above:

```bash
make bootstrap

python experiments/llm_dram3d/run_study.py \
  --prefix gptoss120b_dram3d_square_b1 \
  --cases decode_b1_kv1024 \
  --floorplan square_bands --repeats 1 --build-hardware

python experiments/llm_dram3d/analyze.py \
  --manifest experiments/llm_dram3d/logs/gptoss120b_dram3d_square_b1/study.json \
  --output experiments/llm_dram3d/results/gptoss120b_dram3d_square_b1 \
  --animations --full-maps
```

This produces two co-simulations and eight GIFs: component-average and thermal-cell
animations for both dies and both profiles. Omit `--full-maps` for just the four
component-average GIFs. For a B1/B8 comparison, use
`--cases decode_b1_kv1024 decode_b8_kv1024`; this produces four runs and sixteen
GIFs with the same analysis options. To extend the heating history, use
`--repeats 10` and a new prefix.

Use a new prefix for another study; existing artifacts are not overwritten and
the runner has no resume mode. Run studies sequentially because native builds
and SDK toolchain setup are shared. Omit `--build-hardware` after a compatible
simulator is built. This option builds native models; the runner always compiles
the staged SDK application. `make bootstrap` supplies the thermal solver.

### Rebuild after the Spatz completion fix

The provider's staged native patch corrects a vector-immediate operand lookup
in the Spatz completion check. Without it, the first RMSNorm can stall at
`vmv.v.i` while thermal updates continue. The correction is applied to the
build copy; the SoftHier and SDK checkouts and their revisions stay unchanged.

With the environment above activated, refresh dependencies before rebuilding:
the shared native-patch fingerprint also invalidates the DRAMSys build cache.

```bash
export SIMULATOR_CONFIG="$PWD/SoftHier/soft_hier_sdk/apps_dram3d/config/arch.py"
bash Interface_scripts/providers/softhier/provider.sh bootstrap
bash Interface_scripts/providers/softhier/provider.sh build-hardware
python Interface_scripts/providers/softhier/native/tests/run_spatz_chaining_test.py \
  --output "tmp/spatz_chaining_$(date -u +%Y%m%dT%H%M%SZ)"
```

The focused test uses SDK-generated inputs and checks 32 FP16-to-FP32 results
in both the installed debug and profile/SystemC simulators, with a 60-second
timeout per command. Each must print `SPATZ_CHAINING_PASS` with zero errors.
After validation, use a new study prefix for the paired thermal run; omit
`--build-hardware` when using this rebuilt installation.

With this patch, `decode_b8_kv1024` (layer 1, seed 19, one repeat) passed all
290,368 numerical checks and all 11 stages using profile/SystemC with thermal
coupling disabled. The layer took 25,591,547 cycles (25.592 ms at 1 GHz); the
validation took about 64 minutes on the validation host.

To inspect capacity and addresses before generating large preloads:

```bash
python experiments/llm_dram3d/prepare_workload.py \
  --plan-only --output experiments/llm_dram3d/workloads/capacity_check \
  --cases decode_b1_kv1024 decode_b8_kv1024 prefill_s64 --repeats 1
```

The plan command stages inputs and reports memory layout without compiling or
simulating. Thermal placement is selected by `run_study.py --floorplan`.

## Settings

| Option | Default | Meaning |
|---|---|---|
| `--cases` | `decode_b1_kv1024` | One or more unique SDK presets: `decode_b{1,8,16}_kv{1024,2048,4096}`, `prefill_s64`, or `prefill_s128`. |
| `--repeats` | `1` | Repeat the complete layer execution with the same input and KV-cache position; a positive 32-bit integer. |
| `--interval-ps` | `20000000` | One power/temperature interval for all 11 stages: **20 µs**. |
| `--cells` | `16384` | Approximate thermal cell target per die; the generated grid may differ slightly. |
| `--floorplan` | `redmule_strip` | Original placement or `square_bands`; DRAM channel regions remain aligned with their clusters. |
| `--estimate-scale` | `1.0` | New-logic power coefficient multiplier, as in component-power studies. |
| `--layer` | `1` | SDK layer index, 0–35. Layer 1 uses full attention; layer 0 uses sliding-window attention. |
| `--seed` | `19` | Nonnegative seed for deterministic synthetic inputs and weights. |
| `--all-experts` | Off | Also preload inactive experts, increasing file size without changing the reserved memory mapping. |
| `--thermal-config` | Provider `configs/dram3d_thermal.json` | Repo-local physical package parameters, `stack_order`, and provenance. |
| `--dram-power-config` | Provider `configs/dram3d_power.json` | Repo-local HBM2 electrical parameters and provenance. |
| `--sdk` | `SoftHier/soft_hier_sdk` | Repo-local SDK checkout at the pinned revision. |
| `--prefix` | `dram3d` | New study identifier for logs, workloads, and raw runs. |
| `--build-hardware` | Off | Build native GVSoC models before running the layer. |

KV counts cached past tokens; decode appends one token at that position. Prefill
starts with an empty cache. Repeats accumulate 64-bit stage counters and extend
the thermal history without advancing the token position or performing
autoregressive generation. Numerical validation runs once after the final
repeat. With one repeat, the staged application source is unchanged from the SDK.
Changing repeats requires a software rebuild, which the runner performs.

The thermal JSON's top-level `stack_order` selects `logic_on_dram` (logic on
top) or `dram_on_logic` (DRAM on top, the default when omitted). Edit the
default JSON or pass a complete repo-local copy with `--thermal-config`.
Both choices keep face-to-face bonding, BEOL next to the bond, and cooling
above the top die. Materials and floorplan/channel alignment follow each die.

The thermal integration step is one tenth of the coupling interval. The default
17-layer package runs bottom to top through substrate, bumps, logic bulk/source,
logic BEOL, hybrid bond, DRAM BEOL, DRAM source/bulk, TIM, copper, and top cooling.
Each die retains 49 µm bulk and a 1 µm source, with 5 µm BEOL added for a 55 µm
total die thickness. The hybrid-bond region is 2 µm; the complete package totals
1,242 µm. DRAM BEOL uses a logic-derived placeholder pending DRAM-specific data.
The `logic_on_dram` option reverses the two-die region around the bond while
retaining the same layer count and total thickness.
BEOL heat capacity and hybrid-bond material properties also remain provisional.
The initial and cooling temperatures are **300 K**, with top heat-sink coefficient
**h = 15000 W/(m²·K)** (`1.5e-8 W/(µm²·K)` in 3D-ICE).
See the [model input specification](../../Interface_scripts/providers/softhier/configs/README.md)
for materials, units, and the stack arrangement.
These defaults apply to new exports; completed runs and portable recordings
continue to use their archived stacks.

The tracked [thermal input](../../Interface_scripts/providers/softhier/configs/dram3d_thermal.json)
and [electrical input](../../Interface_scripts/providers/softhier/configs/dram3d_power.json)
are **engineering starting estimates**. Override them with `--thermal-config`
and `--dram-power-config` when characterization is available. Both are validated
before software generation and archived with hashes and resolved values.
Electrical values apply to one complete HBM2 pseudo-channel; command charges
are incremental above background, with no automatic device/stack multiplier.
The default electrical characterization is fixed at 300 K. The two leakage
profiles change logic leakage only; DRAM background can vary over time with
state occupancy but must agree between profiles.

## Files and responsibilities

| File | Responsibility |
|---|---|
| `run_study.py` | Prepare shared inputs, build, run both profiles, validate timing/numerics, and archive provenance. |
| `prepare_workload.py` | Stage architecture/source and repeats, invoke SDK generation/build tools, and prepare a private HBM2 configuration. |
| `analyze.py` | Validate pairs, report logic/DRAM energy and timing, and reuse the existing plots and thermal animations. |
| `thermal_validation.py` | Check feedback, floorplan temperatures, and every raw thermal cell against physical temperature bounds. |
| `test_llm_dram3d.py` | Regression tests for settings, staging, repeats, timing, configuration, pairing, and thermal checks. |

Generated data is Git-ignored:

```text
experiments/llm_dram3d/
  logs/<prefix>/                 study.json, frozen provider, preparation/build/run logs
  workloads/<prefix>/            staged source/configs, per-case layer.elf and preload.elf
  results/<name>/                report.md, PNG/PDF plots, CSVs, GIFs
runs/llm_dram3d/<prefix>_<case>_<profile>/
  generated/                     system contract, floorplans, stack, hook config
  traces/                        power exchanges and component temperatures
  results/3dice/                 thermal_manifest.json and both dies' temperature outputs
  results/simulator/             validation.json
  artifacts/                     executable, preload, SDK manifest, source and configs
```

Open `results/<name>/report.md` first. `metrics.csv` has separate logic/DRAM rows:
`layer_time_us` is the accumulated execution time of all repeats,
`mean_layer_time_us` is the per-layer mean, and `duration_us` covers the whole
simulated application. It also distinguishes component-average peaks from
thermal-cell peaks. `stages.csv` contains the 11-stage accumulated timing and
`mean_duration_us` per repeat. `validation.csv` contains power/leakage residuals.

Other outputs include `component_energy.csv`, `timeseries.csv`, power/temperature
comparison plots, and separate logic/DRAM maps and optional GIFs. CSV
`leakage_or_background` means logic leakage on the logic die and state-dependent
background on the DRAM die. Energies cover the full simulated application,
including all repeats and final validation activity.

## Untimed preload and reproducibility

The parent repo selects SoftHier commit
`a453fa09fe53ff306c91072066ea3ff559a0853a`. The provider pins SDK branch
`chi/soft_hier_dram3d` at `b454aeee4278bb00ecbfcdcc4d1366fe8451d7c8`; architecture
comes from `apps_dram3d/config/arch.py` and source from `llm_dram3d/gpt-oss-120b`.

Each case has one `layer.elf` and one combined `preload.elf` containing distributed
weights, inputs, caches, and gold reference data. Both profiles share these
inputs. The provider uses `--preload-mode=direct`; all seventeen loaders must
complete at cycle zero. Repeats reuse the preloaded data and overwrite the same
output/cache positions without another preload.

The experiment copies the tracked DRAMSys configuration, preserves its native
HBM2 address mapping, enables functional storage and power analysis, and disables
database recording. Staging changes only the copied SDK source for repeated
execution. Run archives include independent copies of binaries, preload, SDK
source, private configs, physical/electrical inputs, and the native power backend
and provider Python helpers used to build it. Copies use filesystem copy-on-write
where available; hashing streams large ELFs. The runner freezes its provider
shell and rejects native/provider source changes during a study.

The study and workload manifests record repeats alongside input/source hashes.
Analysis also accepts earlier manifests that omitted repeats as single-repeat
studies, and rejects combining studies with different repeat or floorplan settings.

For an already activated Python virtual environment instead of Conda, use
`export SOFTHIER_CONDA_ENV=''` and set `PYTHON` to that environment's interpreter.
The repo-local environment at `tmp/dram3d_dev/venv` was used for the initial
validation; it is ignored and is not present in a fresh clone.

## Validation and interpretation

Every run requires successful simulator and 3D-ICE exits, `LLM_DRAM3D_PASS`, the
exact scenario and numerical check count, all **11 stage counters** summing to
the 64-bit cycle total, and zero-cycle ELF preload. Repeated runs also report the
completed repeat count. The runner normalizes split 64-bit stage counters before
calling the staged SDK's unchanged `scripts/run.py:report()` validator.

The SDK checks intermediate/output values, exact route IDs, route weights, and
appended K/V against its reference (`atol=0.003`, `rtol=0.02`). Analysis requires
matching paired binaries/inputs, source/model hashes, numerical logs, stage
timings, power inventories/windows, logic leakage coefficients, and both dies'
thermal files. Every component, floorplan region, and raw thermal cell must be
finite and at least the minimum initial/cooling temperature, allowing 1 mK for
rounding. The two-die stack uses one SuperLU worker; the tested eight-worker
configuration produced unphysical cooling below the initial/boundary temperature.

Power is integrated over exact simulator windows. The final partial window
still advances a full thermal slot, following the existing co-simulation
convention. Stage timing excludes untimed preload and final SDK numerical
validation; application duration includes initialization and validation/reporting.

Synthetic parameters, estimated power/materials, excluded controller/PHY power,
fixed-temperature DRAM characterization, and a cold start limit interpretation.
These runs establish transient behavior for this mapping, not calibrated silicon
power or steady-state temperature. Full workload validation is needed for each
selected case.

Completed study: `b1_kv1024_20261014`, with `redmule_strip` and one repeat.

- Both profiles of `decode_b1_kv1024` passed all 36,296 SDK numerical checks and
  eleven stage counters, taking 10,439,993 layer cycles (10,439.993 µs at 1 GHz).
- All seventeen loaders completed at cycle zero. The combined preload contains
  260,502,336 bytes.
- Each run covers 11,561.363 µs of application time and 579 thermal slots at 20 µs.
  The final 1.363 µs power window advances a complete slot, giving 11,580 µs of
  thermal time. Each die has 16,544 cells for the requested 16,384-cell target.

The [study manifest](logs/b1_kv1024_20261014/study.json) records inputs and numerical
results. Open the [result report](results/b1_kv1024_20261014/report.md) and
[power/temperature comparison](results/b1_kv1024_20261014/decode_b1_kv1024_power_temperature.png)
for measurements; separate logic/DRAM maps and animations are in the same result
directory. These generated files are available in the working checkout only.
Capacity/address plans passed for all eleven SDK cases; other cases' full
numerical and thermal simulations remain unrun.

Maintainer checks:

```bash
make interface-tests
python -m unittest discover -s experiments/llm_dram3d -p 'test_*.py' -v
python -m unittest discover -s experiments/llm_decode -p 'test_*.py' -v
```
