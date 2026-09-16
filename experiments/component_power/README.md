# Component-power experiments: start here

Use this folder to compare **fixed leakage** with **temperature-dependent
leakage** for SoftHier's SDK `implementation/` kernels. Modeled components are
scalar cores, Spatz, RedMulE, SRAM, iDMA, NoC/router interfaces, and transpose logic.

For a normal experiment you only need **two scripts**:

```text
run_study.py  →  logs/<name>/study.json + raw simulation runs
analyze.py    →  results/<name>/report.md + figures + CSVs + optional GIFs
```

Generated `results/`, `logs/`, `workloads/` and the development `WORK_LOG.md`
are local and Git-ignored; they are **not included in commits or pushes**.
Run the workflow below on a fresh checkout to create your own report, figures
and CSVs.

In the development workspace, `results/report.md` and `results/README.md`
describe the local four-kernel `square_bands` result set (one invocation each,
both leakage profiles). That set does not include the optional repeated-workload
or coefficient-sensitivity studies.

## 1. Run your first experiment

Run all commands below from the **repository root**, not this folder.
Use the Python 3.12 environment described in the [root setup guide](../../README.md#os-requirements);
analysis also needs NumPy, Matplotlib and Pillow. The provider expects `py312`
unless configured otherwise in that guide.

```bash
conda activate py312

# First-time dependency/SDK/3D-ICE setup only; skip if already completed.
make bootstrap

# Run RMSNorm twice: once per leakage profile. Use a new prefix each time.
python experiments/component_power/run_study.py \
  --prefix demo_norm --kernels norm --build-hardware

# Validate those runs and generate reports, static plots and both kinds of GIF.
python experiments/component_power/analyze.py \
  --manifest experiments/component_power/logs/demo_norm/study.json \
  --output experiments/component_power/results/demo_norm \
  --animations --full-maps
```

Then open `experiments/component_power/results/demo_norm/report.md`.
Omit `--animations --full-maps` if you only need static plots and numbers.
Omit `--build-hardware` on later runs when a compatible native simulator build
already exists; application software is still built for every selected kernel.
This flag does **not** build 3D-ICE; bootstrap supplies those binaries.

Both runs include thermal simulation and temperature feedback:

| Profile | What changes with temperature? |
|---|---|
| `constant` | Leakage stays at its 25 °C reference value; temperatures still evolve. |
| `temperature_aware` | Leakage is recalculated from the latest component temperature. |

The application binary, preload, dynamic-power model, architecture and floorplan
are identical within each pair. There is no `legacy` profile. The runner always
runs both profiles; it has no single-profile switch.

## 2. Which Python script is for what?

| Script | When you use it | Input → output | Starts a simulator? |
|---|---|---|---|
| [`run_study.py`](run_study.py) | Normal step 1: prepare, build and run paired experiments. | Options below → raw runs, logs, `study.json`. | Yes: GVSoC + 3D-ICE. |
| [`analyze.py`](analyze.py) | Normal step 2: validate runs and make figures/reports. | `study.json` + referenced raw runs → result directory. | No. |
| [`prepare_kernel.py`](prepare_kernel.py) | Helper, called automatically by the runner. Use directly only for custom app/architecture preparation. | SDK kernel + configs → staged app, headers, preload and `workload.json`. Does not compile. | No. |
| [`check_timing.py`](check_timing.py) | Optional diagnostic: check that instrumentation did not change execution time. Requires both `norm` and `acti`. | Archived run manifest/binaries → uncoupled logs and timing CSV. | Yes: GVSoC only. |
| [`summarize.py`](summarize.py) | Optional full-study report: combine baseline, repeated RMSNorm, and low/high SiLU estimates. Not needed for one experiment. | Four analyzed result directories → combined report, comparison/sensitivity CSVs and figure. | No. |
| [`test_analysis.py`](test_analysis.py) | Maintainer regression tests, not an experiment. | Synthetic test cases → pass/fail. | No. |

The helper options and full-study recipe are in [REFERENCE.md](REFERENCE.md).
The optional local `WORK_LOG.md` is development history, not part of setup
or the published repository.
Every command-line tool also explains its options with `--help`, for example:

```bash
python experiments/component_power/run_study.py --help
python experiments/component_power/analyze.py --help
```

## 3. Choose the application and architecture

`--kernels` selects one or more of these SDK applications:

| Name | SDK source under `SoftHier/soft_hier_sdk/implementation/sw/` | Kernel parameters under `implementation/config/kernels/` |
|---|---|---|
| `norm` | `RMSNorm/` | `norm.py` |
| `acti` | `Activation/` (default: SiLU) | `acti.py` |
| `gemm` | `SummaGEMM/` | `gemm.py` |
| `attn` | `FlatAttention/` | `attn.py` |

All four use **`SoftHier/soft_hier_sdk/implementation/config/arch/arch.py`**.
The runner copies that architecture into each generated workload and uses it
for software headers, preload generation, simulation and floorplan export.
It stages generated software outside the SDK and preserves its existing headers.

`run_study.py` has no `--arch`, `--kernel-config` or `--sdk` option; setting
`CFG`/`APP` or `SIMULATOR_CONFIG`/`SIMULATOR_APP` does not replace its selected
workload. For a different app/architecture pair, see the
[custom preparation workflow](REFERENCE.md#custom-applicationarchitecture-preparation).

## 4. Experiment options: `run_study.py`

| Option | Default | What to set / effect |
|---|---|---|
| `--prefix NAME` | `implementation` | A **new** study name. Starts with a letter/digit; remaining characters may also include `_`, `-`, `.`. No paths. Used for logs, workloads and raw-run names. |
| `--kernels NAME [NAME ...]` | `norm acti gemm attn` | Select any subset, without duplicates. Each kernel produces two runs. Start with `norm` for a short workload. |
| `--repeats N` | `1` | Repeat the kernel execution region `N` times within one simulator invocation, with descriptor reinitialization and barriers. Not `N` independent trials. |
| `--interval-ps N` | `1000000` = 1 µs | Power/temperature exchange interval for `norm`, `acti`, `gemm`. Smaller means more feedback points and more runtime overhead. Does not control `attn`. |
| `--attention-interval-ps N` | `20000000` = 20 µs | Exchange interval for `attn` only. |
| `--cells N` | `16384` | Approximate top-die thermal mesh target. More cells resolve finer spatial detail at higher cost; the actual count can differ after discretization. |
| `--floorplan RULE` | `redmule_strip` | `redmule_strip`: original placement. `square_bands`: square cluster with bottom RedMulE/TCDM, middle PE-over-Spatz columns, top support blocks. |
| `--estimate-scale X` | `1.0` | Multiply new-logic power coefficients: cores, Spatz, iDMA, NoC/NI, transpose. Scales activity energy, residual clocks and leakage; **RedMulE/SRAM coefficients and all areas remain unchanged**. |
| `--build-hardware` | Off | Build native GVSoC models before the first kernel, in addition to the software builds that always occur. Use after native power-model/source changes or on the first run. |

Intervals, repetitions and cells must be positive integers; the scale must be
finite and positive. `1 µs = 1,000,000 ps`. The thermal integration step is one
tenth of the selected exchange interval.

Examples after the initial build (each uses a different prefix):

```bash
# All four kernels: eight co-simulations. Omitting --kernels has the same effect.
python experiments/component_power/run_study.py \
  --prefix demo_all --kernels norm acti gemm attn

# Square three-band floorplan; application and component areas stay the same.
python experiments/component_power/run_study.py \
  --prefix demo_square --kernels norm --floorplan square_bands

# Longer heating history: 128 executions in one invocation, sampled every 10 us.
python experiments/component_power/run_study.py \
  --prefix demo_long --kernels norm --repeats 128 --interval-ps 10000000

# Sensitivity to estimated new-logic power, not a statistical confidence bound.
python experiments/component_power/run_study.py \
  --prefix demo_low --kernels acti --estimate-scale 0.5
```

Run these sequentially, not in parallel: native builds, SDK staging and DRAMSys
working files are shared. Analyze each study into its own result directory.
Placement details are in the [floorplan-rule guide](../../Interface_scripts/providers/softhier/floorplans/README.md).

## 5. Result options: `analyze.py`

| Option | Default | Meaning |
|---|---|---|
| `--manifest FILE [FILE ...]` | Required | Runner's `logs/<prefix>/study.json`. This lists raw runs; it is not the raw data itself. Multiple compatible manifests may be combined if they do not duplicate any kernel/profile pair. |
| `--output DIR` | Required | Where to write reports, CSVs and figures. Created if missing; same-named generated files are overwritten if it exists. |
| `--animations` | Off | Add component-average temperature GIFs for both profiles. |
| `--full-maps` | Off | Also add full thermal-cell GIFs, which show gradients inside components. Requires `--animations`. |

Static figures, CSVs and `report.md` are always generated; GIF flags do not
affect simulation physics. Existing raw runs must be available, and the
power-model tables must match those archived with the runs.

Do not combine different floorplan rules, cell targets, repeat counts or
estimate scales in one analysis. Analyze them separately. Both profiles are
required for every kernel. A failed/incomplete pair is not a valid result.

## 6. Where files go, and which ones to open

For `--prefix demo_norm` and the quick-start output directory:

```text
experiments/component_power/
  logs/demo_norm/              Build/run logs, frozen provider, study.json
  workloads/demo_norm/norm/     Staged app, arch.py, preload and software build
  results/demo_norm/           Human-readable report, plots, CSVs and GIFs
runs/component_power/          Raw runs live at repository root, NOT here
  demo_norm_norm_constant/
  demo_norm_norm_temperature_aware/
```

In a result directory (`<kernel>` means `norm`, `acti`, `gemm` or `attn`):

| File | Use it for |
|---|---|
| `report.md` | Read the method, comparison and caveats first. |
| `metrics.csv` | Per-kernel/profile time, power, energy, and peak component/cell temperature. |
| `component_energy.csv` | Energy by component type; separates new-logic residual clocks from activity energy. |
| `timeseries.csv` | Per-component-type power and temperature histories for your own plots. |
| `validation.csv`, `study.json` | Validation residuals and provenance/raw-run paths. |
| `workload_summary.{png,pdf}` | Component dynamic-power breakdown and leakage/total-energy impact. |
| `<kernel>_comparison.{png,pdf}` | Paired temperature, total-power and leakage histories. |
| `<kernel>_leakage_temperature_time.{png,pdf}` | Temperature-aware profile: **time on x**, leakage on the left y-axis, temperature on the right. Panels select the hottest instance of each of six component types. |
| `<kernel>_thermal_maps.{png,pdf}` | End-of-run component-average maps and the temperature difference. |
| `cluster_floorplan.{png,pdf}`, `model_assumptions.{png,pdf}` | Placement and temperature/technology scaling assumptions. |
| `<kernel>_<profile>.gif` | Optional component-average animation. |
| `<kernel>_<profile>_cells.gif` | Optional full thermal-cell animation. |

Use vector PDFs for slides. The paired maps/animations use a common color
scale within each pair. No additional thermal-mapping implementation is needed;
analysis reuses the existing renderer.

## 7. Safe reruns and common problems

- **Existing prefix/run:** choose a new prefix. The runner refuses to overwrite
  logs or raw runs and has no resume mode, including after a failed attempt.
- **Little terminal output:** child build/simulation output is redirected to
  `logs/<prefix>/<kernel>_build.log` and `<kernel>_<profile>.log`.
- **Missing raw files during analysis:** saved PDFs/CSVs are viewable alone,
  but `study.json` cannot regenerate plots without the `runs/` data it references.
  On a fresh checkout, run a new study first.
- **Changed model-table error:** keep the original model revision with archived
  data, or run a new study for changed coefficients; do not mix them silently.
- **Preserving data:** `results/`, `logs/`, `workloads/` and root `runs/` are Git-ignored.
  Keep raw runs and manifests if you want to reanalyze later. These tools do not
  prune results; using the same analysis output directory replaces generated files.

## Interpretation limits

These are first-pass power estimates, not calibrated silicon measurements.
The fixed-leakage reference is 25 °C; initial temperature is 26.85 °C, so some
profile difference exists before self-heating. SDK preloads are timing data,
not independent numerical-correctness tests. Floorplans are area estimates,
and HBM/PHY power is outside the modeled boundary.

See [model tables and provenance](../../SoftHier/pulp/pulp/chips/soft_hier_old/power_models/README.md)
and the [analysis caveats](REFERENCE.md#validation-and-interpretation) before
interpreting numbers. For lower-level single runs and environment controls,
see [co-simulation.md](../../co-simulation.md#important-run-controls).
