# Recorded-block thermal replay

Capture component activity once with GVSoC, then repeat the measured block in
one persistent 3D-ICE process. Temperature-aware replay recomputes leakage every
power slot while retaining the complete die and package thermal state. Startup
runs once and the heating curve begins at the archived initial temperature.

`llm_decode` source runs use the 11-layer single-die thermal package.
[`llm_dram3d`](../llm_dram3d/README.md) source runs use a 17-layer package with
**LOGIC_DIE + DRAM_DIE**. Its thermal configuration selects `dram_on_logic`
(default) or `logic_on_dram` through `stack_order`. Both the single-die and
two-die packages default to **h = 15000 W/(m²·K)**.
`capture` and `record` preserve the source run's physical stack and cooling
coefficient unless `--heat-transfer-coefficient` overrides the latter.
Replay uses the recording's archived stack; changing repository defaults does
not alter existing recordings or results.

## Replay stacked logic/DRAM B1 and B8 workloads

Use the constant-profile source for each batch from an `llm_dram3d` study.
The old B1/B8 recordings below remain single-die inputs; replay does not add a
DRAM die to them. Import a complete source trace with the existing CLI:

```bash
python -m experiments.thermal_replay record \
  --run path/to/constant_source_run \
  --output runs/thermal_replay/recording_dram3d_b1 \
  --layers-per-cycle 1 \
  --layer-start-ps START_PS --layer-end-ps END_PS
```

Replace `START_PS` and `END_PS` with exact absolute timer bounds from that
workload. Omit both options only when the source log contains the two
`THERMAL_REPLAY_ROI_*_PS` markers. Stage cycle totals are durations and cannot
identify the absolute start. Set `--layers-per-cycle` to the source's repeat
count. Repeat the import for B8 with its own source and measured bounds.

If a source lacks a complete hook trace, use `capture --source-run ...` instead
of `record --run ...`; the archive will be under `OUTPUT/recording`. Capture
accepts DRAM3D's `layer.elf`, combined `preload.elf`, and empty optional input
preload. It carries the archived thermal/electrical configurations into the
provider, initializes only logic components to their logic voltage, and requires
the current exported model to match the archived contract.

After creating both recordings:

```bash
(
  set -e
  replay_tag=$(date -u +%Y%m%d_%H%M%S)
  for batch in 1 8; do
    for replay_profile in temperature_aware constant; do
      python -m experiments.thermal_replay replay \
        --recording "runs/thermal_replay/recording_dram3d_b${batch}" \
        --output "runs/thermal_replay/dram3d_b${batch}_${replay_profile}_${replay_tag}" \
        --profile "$replay_profile" --thermal-step 2e-05 --max-seconds 1
    done
    python -m experiments.thermal_replay.analyze \
      --constant-run "runs/thermal_replay/dram3d_b${batch}_constant_${replay_tag}" \
      --aware-run "runs/thermal_replay/dram3d_b${batch}_temperature_aware_${replay_tag}" \
      --output "experiments/thermal_replay/results/dram3d_b${batch}_${replay_tag}" \
      --animations
  done
)
```

The version-2 recording includes `thermal_manifest.json`, both floorplans,
the actual stack with relative floorplan paths, the contract, activity arrays,
and checksums for every required input. It is portable without its source run.
The native source order comes from the contract and manifest: logic then DRAM
for `dram_on_logic`, or DRAM then logic for `logic_on_dram`. Replay inherits
the recorded order; selecting a different order requires a source run exported
with that thermal configuration and a recording from that run.
Each temperature inspection point is checked by filename and region names,
so output ordering cannot silently swap the dies.

Logic uses dynamic power plus constant or temperature-dependent leakage.
For DRAM, the trace's `dynamic_w` is command power and `leakage_w` is
state-dependent background power. Both repeat exactly from the trace, including
changes in background power over time. Neither receives a logic temperature
multiplier. DRAM remains at its archived **fixed electrical characterization**;
replay does not add temperature-dependent DRAM refresh, timing, or leakage.

`summary.json` adds `per_die` energy and temperature totals; `die_timeseries.csv`
adds per-slot die powers and peak component temperatures. Aggregate fields keep
their original names: dynamic includes DRAM commands and leakage includes DRAM
background. Native temperature filenames come from the manifest. The analyzer
adds per-die comparisons, maps, floorplans, and `die_summary.csv`, plus four turbo
GIFs (two dies × two profiles) when requested. Separate die maps share one
temperature scale; overlapping footprints are never drawn on top of one another.

## Run the prepared single-die B1 and B8 workloads

Run commands from the repository root. These recordings are local experiment
data, not files distributed through Git. Copy the portable recording directories
into the paths below, or create recordings from your saved constant-profile
runs before replaying. Both prepared recordings use GPT-OSS-120B,
`repeat=10`, the archived 11-layer package, and **h = 15000 W/(m²·K)**:

| Batch | Portable recording |
| --- | --- |
| 1 | `runs/thermal_replay/recording_b1_block10_h15000` |
| 8 | `runs/thermal_replay/recording_b8_block10_h15000` |

Replay defaults to **1 second** of simulated time, a thermal integration step
equal to the power slot (**20 µs** here), and one factorization thread. One replay
cycle is the complete ten-layer block: `--cycles 10` means 100 layers.

This runs **temperature-aware first, then constant** for each batch and generates
paired figures and turbo GIFs. A timestamp gives each experiment fresh output
directories. Change `for batch in 1 8` to `for batch in 1` or `for batch in 8` to
run only one workload. The subshell stops on a failed run and leaves the calling
shell open.

```bash
(
  set -e
  replay_python=/home/kzhu/miniconda3/envs/py312/bin/python
  replay_tag=$(date -u +%Y%m%d_%H%M%S)
  for batch in 1 8; do
    for replay_profile in temperature_aware constant; do
      "$replay_python" -m experiments.thermal_replay replay \
        --recording "runs/thermal_replay/recording_b${batch}_block10_h15000" \
        --output "runs/thermal_replay/b${batch}_h15000_${replay_profile}_step20us_1s_${replay_tag}" \
        --profile "$replay_profile" \
        --thermal-step 2e-05 \
        --max-seconds 1
    done
    "$replay_python" -m experiments.thermal_replay.analyze \
      --constant-run "runs/thermal_replay/b${batch}_h15000_constant_step20us_1s_${replay_tag}" \
      --aware-run "runs/thermal_replay/b${batch}_h15000_temperature_aware_step20us_1s_${replay_tag}" \
      --output "experiments/thermal_replay/results/b${batch}_h15000_step20us_1s_${replay_tag}" \
      --animations
  done
)
```

Open `report.html` in each analysis directory. Replay alone writes simulation
data; the separate analysis command creates the figures and GIFs. Analysis can
also be run later with the saved output paths.

The original experiment workspace also contains these local B8 results:

- Constant: `runs/thermal_replay/b8_h15000_constant_step20us_1s`
- Temperature-aware: `runs/thermal_replay/b8_h15000_temperature_aware_step20us_1s_retry`
- Report: `experiments/thermal_replay/results/b8_h15000_step20us_1s/report.html`

Historical reports retain the information generated at their original run time.
The current analyzer accepts old and new replay outputs and ignores legacy
convergence fields.

## Run limits and solver settings

The default run ends at `--max-seconds 1`. Add `--cycles N` to request a fixed
number of recorded blocks; the time cap still applies. Increase the cap explicitly
when the requested blocks need longer. Replay does not judge convergence or stop
because temperatures become stable.

`summary.json` reports `time_limit`, `complete` (fixed count or single pass),
`model_limit`, `interrupted`, or `failure`. Logic feedback temperatures above the
archived leakage-table maximum (125 °C here) stop with `model_limit` by default.
`--allow-model-clamp` continues with GVSoC's endpoint leakage clamp; temperatures
are not capped and thermal inputs are unchanged. The summary records
`allow_model_clamp` and `first_model_limit_seconds` (null if never crossed).
Exit codes
are 0 for normal completion or a time cap, 2 for a model limit or failed
validation, 1 for an execution error, and 130 for interruption.

The generated runtime stack uses `transient step 2e-05, slot 2e-05`, even when
the recording archives the older 2 µs integration step. The recording remains
unchanged. `--thermal-step 2e-06` or `1e-05` selects a finer step; it must be
positive and divide the power slot exactly. Leakage still updates once per slot.
Changing the integration step changes numerical accuracy.

The native solver assembles and factorizes its thermal system once. The Python
client sends power and reads component temperatures through a persistent local
socket. Full package-state requests occur only at initialization and final
export. `--factorization-threads 1` is the default because native validation
found intermittent incorrect zero-power solutions with eight SuperLU threads.
The client defaults `OPENBLAS_NUM_THREADS` to 1 unless already set.

## Capture or import another recording

Capture reuses a constant-profile run's ELF, preload artifacts, architecture,
and archived model. It runs GVSoC with a small recording hook and no thermal
solver. The current simulator model must match the archived contract. Use a
separate recording for every batch/input/activity pattern.

The pinned SoftHier submodule does not emit absolute replay timer markers.
Supply both `--layer-start-ps` and `--layer-end-ps` to `capture` or `record`.
Automatic extraction is available only when the simulator independently provides
`THERMAL_REPLAY_ROI_START_PS` and `THERMAL_REPLAY_ROI_END_PS` in its log.

The following exact bounds belong to the two named archived repeat=10 workloads
only. For a different workload, obtain its exact timer bounds first. This example
captures a fresh trace using the existing simulator without changing SoftHier:

```bash
batch=1
case "$batch" in
  1) layer_end_ps=34415614000 ;;
  8) layer_end_ps=117465584000 ;;
esac
capture_tag=$(date -u +%Y%m%d_%H%M%S)
/home/kzhu/miniconda3/envs/py312/bin/python -m experiments.thermal_replay capture \
  --source-run "runs/llm_decode/decode_11layer_b1_b8_h10000_gpt-oss-120b_b${batch}_constant" \
  --output "runs/thermal_replay/capture_b${batch}_block10_h15000_${capture_tag}" \
  --layers-per-cycle 10 \
  --layer-start-ps 447000 \
  --layer-end-ps "$layer_end_ps" \
  --heat-transfer-coefficient 15000
```

The portable recording is created in the capture directory's `recording/`
subdirectory. `capture.json`, `logs/simulator.log`, and the hook trace retain
capture diagnostics.

The prepared B1 recording reuses the archived complete B1 trace. A fresh startup
probe measured its start at 447000 ps and reproduced the captured power samples
exactly. The archived timer duration is 34415167 ns on the sole 1 GHz clock,
giving an end boundary of 34415614000 ps. The probe was stopped after this check;
it is not a second complete capture. Timing evidence is saved in the recording's
`roi_provenance` metadata and `runs/thermal_replay/capture_b1_block10_h15000`.
These measured bounds belong to this specific archived B1 workload.

`--heat-transfer-coefficient` is supported by both `capture` and `record` and
uses **W/(m²·K)**. It changes only the portable recording's top heat sink:
15000 becomes `1.5e-08 W/(µm²·K)` in the stack. The manifest records the original
and new coefficients and hashes the final inputs. Omit the option to retain the
archived stack. Source runs are never edited.

For an existing complete hook trace, use `record --run RUN --output RECORDING
--layers-per-cycle 10`. It requires absolute timer markers in the simulator log,
or both known exact `--layer-start-ps` and `--layer-end-ps`. Older logs lack these
markers, so use the explicit bounds for the archived workloads above. To import
one of those traces without rerunning GVSoC, replace `capture --source-run` in
the example with `record --run` and choose a new output directory. `record`
creates the portable recording directly at `--output`; `capture` places it in
the `recording/` subdirectory. Output directories must be new.

The recording contains power-window timestamps, component dynamic power,
25 °C reference leakage for the modeled memory capacity, exact timer bounds,
the contract, archived model tables, floorplan, stack, and SHA-256 checksums.
It is portable and no longer needs GVSoC or the source workload files.

## Timing, outputs, and analysis

This is a trace of component power averaged over sampling windows. Overlap
integration preserves the recorded energy and exact integer-picosecond period
when block boundaries cut power slots. Repeated blocks are not padded to whole
slots. Run limits round up to the next thermal slot; `stop_rounding_ps` and
`partial_next_layer_ps` record the final rounding and progress into the next block.
The latter retains its historical field name even for multi-layer blocks.

Only the measured timer region repeats; final reporting and output dumps are
excluded. Ten-layer blocks preserve activity variation across the observed
repeat loop. They do not model changed future tokens, temperature-dependent
timing, throttling, DVFS, or changed power models. Leakage uses linear
interpolation between the archived model's temperature samples.

Replay outputs include:

- `summary.json`: stop status, energy, temperatures, timing, recording checksums,
  completed blocks/layers, and effective solver settings; updated during execution.
- `timeseries.csv`: total dynamic/leakage/constant power and peak component
  temperature at each slot end.
- `dynamic_power_w.csv.gz`, `leakage_power_w.csv.gz`, and
  `component_temperatures_c.csv.gz`: component histories. Temperatures include
  the initial state; column order is recorded in `components.json`.
- `cycles.csv`: `cycle,time_ps,layers_completed` at each exact block boundary.
- `final_state.npz`: full solver temperatures, component temperatures, and
  component energy arrays. This is a diagnostic export, not a restart checkpoint.
- `floorplan_temperatures.txt`, `server.log`, and the effective thermal inputs.

`--dense-maps` additionally saves thermal-cell maps at every power slot. It is
unnecessary for the floorplan GIFs. Interrupting replay closes the native server
and retains completed histories plus an explicit status.

The single-die analyzer creates six PNG/PDF figures, metrics and validation CSVs,
an HTML report, and optional paired GIFs. Multi-die runs add the die-specific
outputs described above. It checks matching recording, solver settings,
geometry, duration, and per-component dynamic activity, along with integrated
energy, temperature histories, and map-to-component temperatures. Paired multi-die
reports also check that every DRAM background sample agrees between profiles.
Recordings must remain available. Single-pass validation traces are not supported by this
paired-analysis entry point.

Metrics and the comparison figure use every saved sample. Comparison powers
are plotted as steps without display averaging or shaded bands. Other time
curves use 1 ms means and minimum/maximum envelopes. GIFs use **turbo**, a shared
temperature scale, and matching frames every 10 ms plus endpoint samples at
10 frames/s. Maps show floorplan-region averages. Native rounded timestamps
are reconstructed from row index and the power slot. Matching analysis files
are overwritten if an existing analysis output directory is supplied.

## Validation and prerequisites

Use the simulator Python environment with NumPy, Matplotlib, and Pillow.
Existing installations can use `/home/kzhu/miniconda3/envs/py312/bin/python`.
If native binaries need rebuilding:

```bash
make -C 3D-ICE
Interface_scripts/providers/softhier/provider.sh build-hardware
```

The provider needs its usual dependencies. Set `SOFTHIER_CONDA_ENV=''` when
using an already activated environment. The capture build must include absolute
timer timestamps to extract bounds automatically; with the unchanged SoftHier
revision, provide explicit bounds instead. The thermal server must support the
read-only state command included in the pinned 3D-ICE revision.

For native transport validation, import an old trace with `record --whole-trace`
without a cooling override, then replay it with `--single-pass --profile recorded
--thermal-step 2e-06`. Compare it to its original source with `validate
--replay-output OUTPUT --reference-run SOURCE --single-pass --output REPORT.json`.
This validation mode accounts for the actual final partial power-window duration
while retaining the native full thermal slot.

`validate --constant-run CONSTANT --aware-run AWARE --output REPORT.json` checks
archived co-simulation leakage reconstruction and dynamic-activity equality.
For repeated replay versus co-simulation, match the cooling and integration step
and request the same number of layers: one ten-layer recording cycle corresponds
to a repeat=10 source. Run `validate --replay-output OUTPUT --reference-run SOURCE
--output REPORT.json`; the comparison requires at most 1% component-energy error
and 0.1 °C peak floorplan-temperature error. Short comparisons do not validate an
entire long experiment.

```bash
PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  /home/kzhu/miniconda3/envs/py312/bin/python -m unittest discover \
    -s Interface_scripts/tests -p 'test_*.py' -v
```

Tests cover energy integration, fractional boundaries, explicit stopping limits
under stable temperatures, initial/final state exports, cooling overrides,
legacy analysis compatibility, native/file-mode parity, and interruption cleanup.

## Export and plot power at a saved time

These commands read completed replay outputs; they do not run GVSoC or the
thermal solver. Keep the recording referenced by each run's `summary.json`
available. Use the same Python environment as the analyzer.

To export one chip power map in native 3D-ICE FLP format:

```bash
python -m experiments.thermal_replay.power_maps export \
  --run runs/thermal_replay/b8_h15000_constant_step20us_1s \
  --time-seconds 1 \
  --output runs/thermal_replay/power_maps_t1s/constant
```

The output contains `power_map.flp`, `power_map_regions.csv`, and `metadata.json`.
The FLP preserves region order, positions, dimensions, and discretization, and
assigns one total-power value in **watts** to every region. Fixed regional powers
are included once, separately from component dynamic and leakage power in the CSV.

To compare constant leakage and temperature-aware leakage:

```bash
python -m experiments.thermal_replay.power_maps plot \
  --constant-run runs/thermal_replay/b8_h15000_constant_step20us_1s \
  --aware-run runs/thermal_replay/b8_h15000_temperature_aware_step20us_1s_retry \
  --time-seconds 1 \
  --output experiments/thermal_replay/results/power_maps_t1s
```

This writes `power_maps.png`, `.pdf`, and `.svg`, both profile FLPs
(`constant_power_map.flp` and `temperature_aware_power_map.flp`),
`power_map_regions.csv`, and `plot_metadata.json`. Both panels share one linear
**turbo** scale for **power per region in mW**. Headers show total power in W,
chip-average power density in W/mm², and absolute dynamic/leakage powers in W.
Nonzero fixed power is shown separately. Average density uses the full chip
footprint from the runtime stack, not the sum of selected component areas.

For multi-die runs, both commands process all dies by default and put their
outputs in `logic_die/` and `dram_die/` subdirectories. Add `--die LOGIC_DIE`
or `--die DRAM_DIE` to select one. The selected die's total power and density
use that die's power over the common footprint; package totals remain in the
metadata. DRAM panels label their split **Commands / Background**. All selected
dies and profiles share the same turbo power scale. No multi-die powers are
collapsed into a single overlapping FLP.

`--time-seconds` defaults to 1 and must match a saved power-slot endpoint exactly.
For a 20 µs slot, the sample at 1 s is the mean power applied over
**0.99998–1.00000 s**. No interpolation or temperature-based power reconstruction
is performed. Gzip histories are streamed to the selected row. The commands check
archive hashes, component mapping, geometry, solver timing, and regional totals
against `timeseries.csv`; paired plots also check equal dynamic activity at the
selected time and identical thermal stacks. Metadata records the interval,
units, totals, density, and source-file hashes. These commands require repeated
replays; single-pass traces may contain partial power windows and are rejected.

For B1, substitute the paths of your completed B1 replay pair. All dimensions,
powers, and cooling settings are read from the inputs. Output directories must
be outside the source runs and recording; matching generated files are
overwritten when rerunning a command. Generated figures and experiment data
remain ignored by Git.

## Plot the thermal layer stack

```bash
python -m experiments.thermal_replay.plot_stack \
  --stack runs/thermal_replay/b8_h15000_temperature_aware_step20us_1s_retry/ice.stk \
  --output experiments/thermal_replay/results/layer_stack_h15000
```

The command reads the actual saved stack and writes `layer_stack.png`, `.pdf`,
`.svg`, and `.json`. The figure uses equal-height schematic blocks, with the
layer name and physical thickness inside each block and the chip footprint
inside the bottom block. Consecutive compatible TIM and PCB layers are merged
for presentation only. The current stack becomes Heatsink (50 µm), TIM (30 µm),
Silicon die (50 µm), Bumps (50 µm), and PCB (1000 µm). JSON retains all 11 physical
layers, material properties, cooling settings, source hash, and schematic groups.
Source-plus-bulk die definitions are supported and merged into one schematic
block per die; BEOL is shown separately. The default memory-on-logic package
has 17 physical layers, 1242 µm total: Heatsink (50 µm), TIM (30 µm), DRAM
silicon (50 µm), DRAM BEOL (5 µm), Hybrid bond (2 µm), Logic BEOL (5 µm), Logic
silicon (50 µm), Bumps (50 µm), and PCB (1000 µm). Each die totals 55 µm including
its BEOL. For `logic_on_dram`, logic silicon/BEOL appear above the bond and
DRAM BEOL/silicon below it; layer count and total thickness stay the same.
JSON retains each physical source/bulk/BEOL sublayer. Unsupported
elements and ambiguous source definitions are rejected instead of omitted.

Existing recordings keep their archived physical stack, including the previous
15-layer, 1235 µm memory-on-logic package. Neither `replay` nor `record` imports
the current provider defaults into an older run or recording. The plot always
shows the supplied stack file's actual layers.

The main analyzer's comparison figure has four panels: peak temperature, total
power, constant-case dynamic/leakage power, and temperature-aware dynamic/leakage
power. The two power-decomposition panels share a y-axis scale.
