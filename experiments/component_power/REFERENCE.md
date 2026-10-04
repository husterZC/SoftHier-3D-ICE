# Optional helpers and full-study recipe

Start with [README.md](README.md) for the normal `run_study.py` → `analyze.py`
workflow and their complete option tables. This page covers the remaining
tools. Commands assume the repository root, an active `py312` environment,
completed bootstrap and a compatible native simulator build.

## Custom application/architecture preparation

`prepare_kernel.py` is called automatically by `run_study.py`; do not run it
first for a normal study. Its standalone interface is for preparing a different
SDK implementation configuration.

| Argument | Default | Meaning |
|---|---|---|
| `kernel` (positional) | Required | `norm`, `acti`, `gemm` or `attn`. |
| `--output DIR` | Required | Staging directory. Writes `app/` (including generated headers and `preload.elf`) and `workload.json`. Existing files can be overlaid; choose a fresh path. |
| `--sdk DIR` | `SoftHier/soft_hier_sdk` under this repository | SDK checkout containing implementation sources and generators. |
| `--arch FILE` | `<sdk>/implementation/config/arch/arch.py` | Architecture used by the preload generator; also pass this same file to the subsequent simulator build/run. |
| `--kernel-config FILE` | `<sdk>/implementation/config/kernels/<kernel>.py` | Kernel dimensions/options used for generated headers and input preload. |
| `--repeats N` | `1` | Positive execution-region repetition count in the staged app source. |

Example using the default files explicitly, so you can see where to substitute
your own compatible configurations:

```bash
python experiments/component_power/prepare_kernel.py norm \
  --arch "$PWD/SoftHier/soft_hier_sdk/implementation/config/arch/arch.py" \
  --kernel-config "$PWD/SoftHier/soft_hier_sdk/implementation/config/kernels/norm.py" \
  --output "$PWD/experiments/component_power/workloads/custom_norm"

make coupled-run RUN_NAME=custom_norm \
  SIMULATOR_CONFIG="$PWD/SoftHier/soft_hier_sdk/implementation/config/arch/arch.py" \
  SIMULATOR_APP="$PWD/experiments/component_power/workloads/custom_norm/app" \
  SIMULATOR_PLATFORM="$PWD/experiments/component_power/workloads/custom_norm/app/preload.elf" \
  SOFTHIER_SW_BUILD="$PWD/experiments/component_power/workloads/custom_norm/sw_build" \
  SOFTHIER_POWER_PROFILE=temperature_aware \
  SOFTHIER_FLOORPLAN=square_bands \
  POWER_INTERVAL_PS=1000000 ICE_TARGET_TOP_DIE_CELLS=16384
```

Preparation alone does **not** compile or simulate. The root `make coupled-run`
command builds and runs the selected app/architecture. Absolute paths matter
because the provider changes working directories. If using a different SDK,
the provider's SDK selection must also be configured consistently; consult the
[provider guide](../../Interface_scripts/README.md).

This lower-level workflow creates one ordinary co-simulation run, **not** a
`run_study.py` paired-study manifest. Do not pass its `workload.json` to
`analyze.py`; that file describes preparation, not simulation results. Use the
[root run/plot workflow](../../co-simulation.md) for these custom runs.
`run_study.py` does not consume previously staged custom apps.

## Instrumentation timing check: `check_timing.py`

This optional diagnostic reruns archived RMSNorm and SiLU executables with
power/thermal instrumentation disabled, then compares their completion times
against the coupled runs. It runs GVSoC but neither rebuilds software nor starts
3D-ICE. Run it sequentially with other simulations.

| Option | Default | Meaning |
|---|---|---|
| `--manifest FILE` | Required | One `study.json`, containing exactly one `constant` run each for `norm` and `acti`. Other kernel/profile entries are ignored. |
| `--logs DIR` | Required | New directory for diagnostic logs and a frozen provider copy. Must not already exist. |
| `--output FILE` | Required | CSV **file**, not directory. Parent directories are created; an existing file is replaced. |

For example, after running `demo_all` from the main guide:

```bash
python experiments/component_power/check_timing.py \
  --manifest experiments/component_power/logs/demo_all/study.json \
  --logs experiments/component_power/logs/demo_timing \
  --output experiments/component_power/results/demo_timing.csv
```

A `norm`-only study is insufficient. The original raw-run logs and archived
ELF/preload/architecture files must remain available. A timing mismatch causes
an error; successful output has `difference_ns = 0` for both kernels.

## Combined presentation report: `summarize.py`

Use `analyze.py` for a single study's report. `summarize.py` is specifically for
the **four-study protocol below**, not a general merger of arbitrary results.
It reads previously analyzed CSVs/manifests and runs no simulators.

| Option | Default | Required analyzed result directory |
|---|---|---|
| `--baseline DIR` | Required | All four kernels, `--repeats 1`, `--estimate-scale 1`. |
| `--sustained DIR` | Required | `norm`, `--repeats` greater than 1, `--estimate-scale 1`. |
| `--low DIR` | Required | `acti`, `--repeats 1`, `--estimate-scale 0.5`. |
| `--high DIR` | Required | `acti`, `--repeats 1`, `--estimate-scale 2`. |
| `--output DIR` | Required | Writes `report.md`, `comparison.csv`, `sensitivity.csv` and `feedback_and_sensitivity.{png,pdf}`. Same-named files are replaced. |

All four input directories must contain `study.json`, `metrics.csv`,
`component_energy.csv` and `validation.csv` from `analyze.py`. They must use the
same floorplan rule and cell target. If `<output>/timing.csv` exists, it is
checked and included in the report; otherwise timing diagnostics are optional.

### Fresh full-study recipe

These are **seven pairs / 14 co-simulations**, considerably more work than the
quick start. Run the commands sequentially. Names below are new example names;
change them if you have used them before. These commands explicitly select
`square_bands`. To study the original layout, use `--floorplan redmule_strip`
on **all four** runner commands and keep the results separate. The runner's
default remains `redmule_strip` when the option is omitted.

```bash
python experiments/component_power/run_study.py \
  --prefix slides_main --kernels norm acti gemm attn --floorplan square_bands --build-hardware
python experiments/component_power/run_study.py \
  --prefix slides_long --kernels norm --floorplan square_bands --repeats 128 --interval-ps 10000000
python experiments/component_power/run_study.py \
  --prefix slides_low --kernels acti --floorplan square_bands --estimate-scale 0.5
python experiments/component_power/run_study.py \
  --prefix slides_high --kernels acti --floorplan square_bands --estimate-scale 2

# After all studies succeed, analyze each independently.
for study in slides_main slides_long slides_low slides_high; do
  python experiments/component_power/analyze.py \
    --manifest "experiments/component_power/logs/$study/study.json" \
    --output "experiments/component_power/results/$study" \
    --animations --full-maps
done

python experiments/component_power/summarize.py \
  --baseline experiments/component_power/results/slides_main \
  --sustained experiments/component_power/results/slides_long \
  --low experiments/component_power/results/slides_low \
  --high experiments/component_power/results/slides_high \
  --output experiments/component_power/results/slides_report
```

Omit `--animations --full-maps` for static-only results; the combined report's
animation links then have no generated GIFs to open. If timing controls are
desired, run `check_timing.py` on `logs/slides_main/study.json`
before summarizing, with a fresh `--logs` directory and
`--output experiments/component_power/results/slides_report/timing.csv`.

The report text describes the pinned SDK architecture/kernel presets and the
reference coupling intervals above. It is not a generic report template for
arbitrary custom architectures or time bases; review its method section if
you change the protocol. Coefficient multipliers are assumption-sensitivity
cases, not statistical confidence intervals. Mesh/interval convergence needs
separate studies.

## Maintainer tests: `test_analysis.py`

Tests use small synthetic fixtures; they do not run hardware simulations:

```bash
python -m unittest discover -s experiments/component_power -p test_analysis.py -v
python -m unittest discover -s Interface_scripts/tests -q
```

The first covers analysis validation, energy decomposition, sensitivity
boundaries and CLI misuse. The second covers the shared interface, preparation,
power models and floorplan rules. SDK-dependent tests may skip before bootstrap.

## Validation and interpretation

Analysis checks byte-identical paired binaries/preloads, geometry and mappings,
simulation duration/windows, per-window dynamic power, fixed control leakage,
and temperature-dependent new-logic leakage against the configured tables.
Component power domains must be disjoint and cover the traced inventory.
These checks establish consistency with the model, **not** silicon accuracy.

The constant profile is referenced at 25 °C while the initial thermal condition
is 26.85 °C. The combined report separates this initial offset from subsequent
self-heating and reports within-run leakage growth separately. A short run can
show an energy difference without appreciable temperature divergence; these
are transient runs, not steady-state measurements.

Component-average temperatures differ from maximum finite-volume cell
temperatures; `metrics.csv` reports both. Tflp temperatures have approximately
0.001 K output precision. The renderer reconstructs rounded timestamps from
the known slot length without changing temperatures. Energy uses the exact
final partial GVSoC interval, but the thermal solver advances it as a full
slot, quantizing its endpoint time by at most one exchange interval.

SDK preloads are placeholder timing data, not independent numerical golden
tensors. Successful termination is not proof of kernel numerical correctness.
Fast scalar/vector models are supported; the exporter rejects the accurate-core
variant. HBM/PHY and uncharacterized local support logic are outside the
modeled power boundary; the floorplan is not a placed-and-routed design.

This pipeline reuses the existing thermal mapping, solver and animation code.
It does not change the 3D-ICE solver or perform commits, branch changes or pushes.
