# Closed-loop simulation overhead

This experiment compares architecture-only SoftHier with temperature-aware
closed-loop simulation at whole-stack XY pitches of 100, 50, and 20 µm.
It measures the cost of the current implementation on `decode_b8_kv1024`.
Grid accuracy and convergence analysis are deferred.

## Run

Run from the repository root using the `py312` environment. Previewing a plan
does not create study artifacts, build software, or start a simulator:

```bash
python -m experiments.closed_loop_overhead plan --prefix b8_overhead
```

Build the native timing probes and run the complete experiment:

Inline power-accounting timing is always enabled in this runner.
`--power-accounting-timing` remains accepted for existing commands; no flag is
needed. Rebuild hardware once after updating the provider patches. For one
measured block, use `--replicates 1`. Choose an unused prefix for a manual run.

```bash
python -m experiments.closed_loop_overhead run \
  --prefix b8_overhead_optimized \
  --coupling-mode optimized \
  --grid-pitches-um 100 50 20 \
  --interval-ps 20000000 \
  --replicates 5 \
  --build-hardware
```

The runner prepares one workload, builds outside the measurements, performs
one pilot for each configuration, then executes five randomized blocks of the
four configurations: 20 measured runs plus four pilots. Each run starts new
processes and resets temperatures. Full B8 executions can take hours; this is
a multi-run study, not a short test. No one-second replay is involved.

Results are stored under `experiments/closed_loop_overhead/results/<prefix>`.
The runner refuses to overwrite a study. Resume with identical settings and
source files using `--resume`; completed runs are skipped and failed attempts
are preserved under suffixed directories. Do not pass `--build-hardware` on
resume unless the installed binaries need restoration: differing native
artifact hashes require a new study. A preparation failure before a workload
is saved requires a new prefix.

`--workload PATH/workload.json` reuses a compiled or prepared workload only if
its B8 case, layer, seed, repeat count, thermal/power inputs, and executable
hashes match. The default layer is 1, seed is 19, and workload repetition is 1.
`--replicates` controls independent host runs, not guest workload repetition.

Use `--cpus 0,2,4,6` to select allowed CPUs explicitly. The default selects up
to four physical cores on one NUMA node/socket, avoiding sibling SMT threads.
The same affinity is used for every measured configuration. Do not run other
experiments concurrently. Optional `--max-host-seconds N` stops an overlong
run and records a failure; it never treats the shorter elapsed time as a result.

Analysis can be regenerated without simulating:

```bash
python -m experiments.closed_loop_overhead analyze \
  experiments/closed_loop_overhead/results/b8_overhead_optimized
```

### New CPU backend with one-thread AMGCL CG

The original server and SuperLU remain the default. To use the new backend,
select it through the experiment's arguments; the runner intentionally clears
inherited `DICE_*` and `ICE_*` environment variables. The new CPU server can be
built separately before any measurement:

```bash
make 3dice-build DICE_DIR="$PWD/3D-ICE_30_Oct" DICE_BUILD_TARGET=cpu
```

Run the closed-loop study first:

```bash
/home/kzhu/miniconda3/envs/py312/bin/python \
  -m experiments.closed_loop_overhead run \
  --prefix b8_overhead_amgcl1 \
  --dice-dir "$PWD/3D-ICE_30_Oct" --dice-build-target cpu \
  --solver-type cpu_amgcl_cg_amg --solver-threads 1 \
  --solver-tolerance 1e-6 \
  --grid-pitches-um 100 50 20 --interval-ps 20000000 \
  --replicates 1
```

This schedules four pilots and four measured executions: architecture only,
then the three thermal configurations in randomized order within each block.
They use 18,107, 67,139, and 396,347 cells. All thermal configurations use
temperature-aware leakage and a 20 µs step equal to the power-update interval.
BLAS uses one thread. `1e-6` is an absolute linear-system residual tolerance,
not a bound on temperature error. Full grid-accuracy analysis remains separate.

After that study completes, run the matching capture/replay overhead experiment:

```bash
/home/kzhu/miniconda3/envs/py312/bin/python \
  -m experiments.closed_loop_overhead.replay_overhead run \
  --reference-study experiments/closed_loop_overhead/results/b8_overhead_amgcl1 \
  --prefix b8_replay_amgcl1_50um_1s \
  --max-seconds 1 --replicates 1
```

Replay inherits the selected CPU server, AMGCL CG, one thread, and tolerance
from the reference study. It runs 50,000 steps on the 67,139-cell mesh. One
pilot workflow and one measured workflow each perform a fresh SoftHier capture
followed by replay; only the measured workflow contributes to the results, and
its capture cost is charged once. Use an unused prefix; old results are kept.

Replace `run` with `plan` to preview either command without starting a study.
The replay plan requires the completed reference. Add `--resume` to the same
run command to continue an identical study. Changed binaries, sources, or
solver controls require a new study. `--build-hardware` on the closed-loop
runner also builds the selected thermal target; replay never rebuilds its
reference binaries.

Backend/solver arguments are also accepted by the closed-loop `smoke` command.
For a bounded check of all three grids, use the same five backend/solver options
with `smoke --output runs/overhead_amgcl_smoke --replicates 1
--grid-pitches-um 100 50 20`. Replay smoke inherits the reference selection:
`python -m experiments.closed_loop_overhead.replay_overhead smoke
--reference-study experiments/closed_loop_overhead/results/b8_overhead_amgcl1
--output runs/overhead_amgcl_replay_smoke`.

The study records the selected sources, native binary hashes, and effective
solver controls. AMGCL emits one `preconditioner_setup` event instead of an LU
`factorization`. Both are initialization costs, excluded from the workload/replay
bars and included in end-to-end time. Reports retain separate factorization and
preconditioner counts/durations. Existing SuperLU studies remain readable.

## Fixed physical and numerical settings

The default is the current 17-layer `dram_on_logic` stack with `square_bands`
placement, nominal voltage/frequency, 300 K initial and ambient temperature,
and cooling coefficient 15,000 W/(m² K). 3D-ICE and BLAS use one thread.

The thermal integration step **equals the power-update interval**. The default
is 20 µs for both, producing `transient step 2e-05, slot 2e-05`. The benchmark
derives both settings from `--interval-ps`; there is no separate timestep knob.
A positive final partial power window still advances one full native slot,
following the existing coupled protocol. Raw spans retain that window length.

The requested pitch is a maximum XY cell dimension, not a cubic cell size.
Each region uses `ceil(width/pitch) × ceil(height/pitch)` cells, preserving its
boundaries, full area, source power, and existing vertical layer thickness.
Every passive layer is refined too. The default geometry produces:

| Pitch | Total thermal cells |
|---|---:|
| 100 µm | 18,107 |
| 50 µm | 67,139 |
| 20 µm | 396,347 |

The generated `mesh.json` reports actual dimensions and per-layer counts.
Native cell counts must agree before a run is accepted. Shared generation
supports `--grid-pitch-um` / `ICE_GRID_PITCH_UM` for version-2 physical-layer
contracts. Explicit target-cell and pitch inputs conflict; the legacy
target-cell interface and its defaults remain available when pitch is absent.

The benchmark writes feedback temperatures every exchange and full maps only
at completion. GIF generation is disabled. Thermal inputs and outputs remain
available for later analysis, but this study makes no grid-accuracy claim.

## Timing and interpretation

Architecture-only runs disable the hook, SoftHier power accounting, and DRAMSys
`PowerAnalysis`. The effective power/debug/profile flags are saved for every
run: this target can already select the profile backend in architecture-only
mode. The headline comparison is the **total cost of enabling closed-loop
simulation**, including inline power bookkeeping and any execution-mode change.

Two wall-time scopes are reported:

- End to end: per-run launch/preparation through both simulators' completion,
  including initialization and numerical checking. Shared builds, workload
  generation, plotting, and archival copies are excluded.
- Workload ROI: native host timestamps at the existing timer MMIO writes.
  The B8 guest executable is unchanged. Initialization and post-ROI checking
  appear outside this interval.

`CLO_TIMING_DIR` enables optional native/Python JSONL spans. It is unset for
ordinary runs. Each process has a separate file, uses `CLOCK_MONOTONIC` /
`time.monotonic_ns`, and records its PID, simulated time, CPU use, and peak RSS.
Power exchanges additionally record their window duration; thermal steps
record native simulated time. The native event files contain matrix cell/NNZ
counts, factorization or preconditioner setup, source/RHS/solve times, ROI
markers, and process exits.

The phase analysis partitions wall time exactly once. Inside ROI, thermal
advancement takes precedence over overlapping hook waits, followed by power
capture and coupling/output. Remaining time is architecture execution,
from which the directly timed inline-power counter is subtracted. That duration
becomes the separate `inline_power_accounting` phase; small unclassified
instrumentation costs remain in architecture execution.
Time before/after ROI is initialization/completion. `detail_seconds` retains
nested diagnostic spans; those totals must not be added to the exclusive
phase totals.

`CLO_POWER_ACCOUNTING_TIMING=1` enables the direct native timer alongside
`CLO_TIMING_DIR`; the experiment sets both automatically. The provider's staged
patches time logic energy/leakage updates, power-source state checks,
power-maintenance callbacks and their scheduler calls, and DRAMPower command
processing. One shared accumulator counts only outermost calls, including
callbacks across the DRAMSys library boundary. Hook exchanges are excluded so
capture, energy snapshots, feedback updates, and blocked thermal waits retain
their existing attribution. Generic scheduler dispatch outside power routines
remains in architecture execution. This is direct time in the stated routines,
not a causal estimate of every slowdown introduced by enabling power.

Counters are accumulated in memory and written once at workload completion as
zero-duration `inline_power_accounting` records (`kind=counter`, method version 1).
Their `value` is accumulated nanoseconds, with call counts and matching host/guest
window boundaries. `timing.power_accounting` preserves this information in each
measurement. No per-call logging occurs. The timer requires the current single
simulation-thread execution path; concurrent accounting threads are rejected
instead of summing overlapping wall time. Timings include probe overhead; no
estimated overhead correction is subtracted.

Historical measurements lack this field; timer-off calibration runs record
`power_accounting=null`. Their architecture phase still includes inline work.
Missing counters are not interpreted as zero. Changed source or native build
identities prevent resuming an incompatible study through the existing checks.

The default `--coupling-mode optimized` retains the file-based thermal transport,
but uses **5 host milliseconds** for both polling intervals. One persistent Python
worker loads configuration and mappings once and reads each temperature row once.
A small compiled client uses the existing executable-hook interface to contact
that worker; SoftHier itself is unchanged. Each run starts a fresh worker.

`--coupling-mode legacy` retains a Python process per exchange, full-history
temperature parsing, **1 host second** native polling, and **20 host milliseconds**
adapter polling. Explicit `--dice-poll-seconds` and `--adapter-poll-seconds` override
the selected mode's host intervals. These settings never change the simulated
thermal step or power interval. Custom `POWER_HOOK_EXECUTABLE` integrations use
`COUPLING_MODE=legacy` when invoked through `make coupled-run`.

For a legacy comparison, use a separate prefix:

```bash
python -m experiments.closed_loop_overhead run \
  --prefix b8_overhead_legacy_new \
  --coupling-mode legacy \
  --grid-pitches-um 100 50 20 \
  --interval-ps 20000000 --replicates 5 --build-hardware
```

Legacy result formats remain readable by the analyzer. Existing studies cannot
be resumed with changed sources. Run comparisons sequentially.

The compiled hook client and 3D-ICE client/server hashes are recorded alongside
the native simulator hashes. Helper compilation occurs before timed runs.
`state/adapter_diagnostics.json` records worker status, configuration-load count,
rows/bytes read, bounded buffer usage, and worker CPU time. Worker/socket failures
fail the run; an ambiguous exchange is never retried automatically. Interrupted
runs restart with fresh thermal state through the existing study-resume workflow.

`timing.coupling_diagnostics` reports power-publication-to-solver delay and nested
adapter stage times. Nested times and client/worker round trips overlap thermal
work; do not add them to the exclusive phase totals. Direct inline timing is
separate from `power_capture`, which includes deferred DRAM energy queries.

Resources are per process. Do not sum independent peak RSS values and describe
that sum as peak simultaneous memory. Exchange latency samples describe one
run; they are not independent benchmark repetitions.

## Outputs and statistics

`study.json` records the schedule, host/configuration metadata, revisions,
source and executable hashes, native artifact hashes, and architectural cycle
signature. `source_snapshot/` preserves the experiment and integration sources.
Each run contains commands, logs, raw timing spans, validation, and
`measurement.json`. Interrupted and failed runs are retained explicitly.

Only complete measured blocks enter comparisons. `analysis.json` records how
many requested blocks completed and which runs are missing or failed.
`summary.csv` reports mean seconds, ratio-of-means slowdown, overhead percentage,
and 95% percentile intervals from 10,000 whole-block bootstrap resamples. One
complete block permits descriptive results but has no confidence interval.
All individual run times remain in `runs.csv`.

The PDF/PNG figures are `runtime` (absolute time and slowdown for both scopes),
`phases` (exclusive wall-time breakdown), and `mesh_cost` (runtime, solver cost
per step, and 3D-ICE peak RSS against native cell count).

### Publication figure for the optimized study

Generate a single-panel workload-execution figure from saved measurements,
without running a simulator:

```bash
python plot_script/experiment_4_closed_loop_overhead/plot_closed_loop_overhead.py
```

The default input is `results/b8_overhead_optimized` under this experiment.
The script and default exports are in `plot_script/experiment_4_closed_loop_overhead/`.
Default paths resolve relative to the script, so an absolute script invocation
works from any working directory. Override input or destination with
`--study /path/to/study --output-dir /path/to/figures`; use `--dpi` to change
the default 600-dpi PNG resolution.

`closed_loop_overhead.pdf` and `.svg` are vector figures; `.png` is the raster
version. The `.csv` records exact plotted values and the `.json` records input
hashes, selected/excluded runs, timing definitions, font identities, and plotting
settings. `closed_loop_overhead_caption.txt` supplies the figure caption.
Times New Roman regular/bold fonts are required. The script searches the local
font caches beside this script and the layer-stack plot, plus installed fonts;
`--font-dir` supplies a different font directory. PDF fonts are embedded and SVG
text is outlined for portability.

The layout is 3.6 × 2.15 inches for a single journal column. Muted solid fills,
light gridlines, and a shared label for the three closed-loop grids keep the
breakdown readable at that size. Bar labels show total minutes in bold and
relative overhead underneath. Fill colors and the style version are recorded
in the JSON metadata; all segment heights remain proportional to measured time.

The figure shows workload execution time in minutes, excluding initialization
and finalization. Percentages use the architecture-only workload baseline.
The legend labels are SoftHier/GVSoC (blue), Power estimation (yellow), 3D-ICE
(orange), and Others (green). Others contains coordination and output time.
Yellow combines directly measured inline accounting with capture and
mapping. Raw mapping intervals are repartitioned using the original overlap
priorities; nested durations are never added twice. CSV/JSON also retain the
inline and capture/mapping durations separately. Baseline bookkeeping is plotted
when measured, rather than forced to zero. Setup/factorization is outside ROI.

The combined figure requires a completed run with direct counters. Point
`--study` at the results of a completed manual run. Historical results without
direct counters produce an explanatory error before writing outputs. Missing
timings cannot be backfilled into old measurements.

Only complete measured blocks with numerical PASS and matching cycle signatures
are plotted; pilots and incomplete blocks are excluded. A study with
one measured repeat per case has no error bars. Studies with multiple
complete blocks use mean runtimes and paired-block bootstrap confidence intervals.

Focused plot-accounting checks:

```bash
python -m unittest discover -s plot_script/experiment_4_closed_loop_overhead -p 'test_*.py'
```

## Validation

Run focused streaming/worker checks and a native equivalence/performance check:

```bash
make -C 3D-ICE -j4
python -m unittest Interface_scripts.tests.test_optimized_coupling
python -m experiments.closed_loop_overhead.validate_coupling \
  --output experiments/closed_loop_overhead/results/coupling_validation_new
```

The latter runs a short actual SoftHier kernel in both modes, leakage profiles,
and stack orders, then a synthetic 128-exchange producer against native 3D-ICE
at 100 µm. It checks identical power rows, temperature feedback, final maps, and
guest cycles, and records whether average coordination is below 100 ms/exchange.
Run it in isolation. This is a transport check, not B8 performance or a confidence
interval. Full B8 measurements are launched separately using the commands above.

Focused tests, including native thermal checks when the server is built:

```bash
python -m unittest experiments.closed_loop_overhead.test_overhead
```

After building hardware, a small arithmetic kernel exercises actual uncoupled
and coupled paths, ROI timers, power enable/disable, final maps, and timing
accounting. It also records indicative instrumentation-on/off timing ratios:

```bash
python -m experiments.closed_loop_overhead smoke \
  --output experiments/closed_loop_overhead/results/native_smoke_check \
  --replicates 3
```

Both smoke variants retain the existing phase timers; the on/off comparison
isolates the new inline timer. Guest cycles, power rows, temperature feedback,
and final maps must match. These short checks are not B8 performance results.
Their on/off ratios include
host noise and polling effects; report them as a calibration, not a guaranteed
instrumentation overhead bound. Main B8 measurements require numerical PASS,
identical guest cycle counts, matching native/generated mesh counts, one
factorization, and one thermal step per positive-duration power exchange.

## Bounded diagnosis of inline power accounting

Historical `SoftHier` timings include energy bookkeeping performed inside the
simulator. To diagnose that cost separately from thermal coupling, reuse a completed B8 study and run
four configurations with no power hook or 3D-ICE: architecture only, logic power
only, DRAM power only, and both. These are intentionally partial executions;
they do not reach the workload's final numerical validation.

```bash
python -m experiments.closed_loop_overhead.power_diagnosis \
  --reference-study experiments/closed_loop_overhead/results/b8_overhead_optimized \
  --output experiments/closed_loop_overhead/results/b8_power_diagnosis_new \
  --start-us 1000 --window-us 100
```

Each case starts a fresh simulator with the recorded executable and preload.
The default measures host wall and process CPU time for simulated time
1.0–1.1 ms, excluding startup and warmup. It then samples native instructions
during 1.1–1.2 ms in the same execution. The first window has no sampler;
sampling overhead belongs only to the second window. All cases use the profile
backend, disabled waveform output, identical CPU affinity, and the initial
power-model temperature without thermal feedback. `logic_only` controls GVSoC's
global power flag; `dram_only` controls DRAMSys `PowerAnalysis`. Some native
models still perform bookkeeping with global power disabled, so the baseline
need not contain zero power-function samples.

The HBM2 backend queues command records during execution and integrates energy
when queried. With no hook, these windows measure its inline command path;
deferred integration happens at shutdown, outside both windows. Consequently
DRAM-only timing is not the complete cost of DRAM energy estimation, and its
queue history differs from closed-loop execution. The full study's
`power_capture` spans already include deferred energy queries.

The runner builds a small SystemC launcher in the output directory, linked to
the installed profile engine with matching compile definitions. Its stepping
loop follows the existing SystemC driver and adds exact simulated-time stops.
It does not edit SoftHier sources or replace installed binaries. Boundary
markers contain cycle counts and native clocks; the process pauses while the
controller collects metadata. This diagnostic harness is shared by all four
cases, but its absolute times should not replace the full study's timings.

Requirements are the built, instrumented simulator, its staged source headers,
a C++ compiler, `nm`, Linux x86-64, and permission to use `perf_event_open` for
owned processes. No `perf` executable is needed. `--no-sampling` allows timing
without perf permissions. `--timeout` limits each advancement to a boundary
(default 900 host seconds), and the output directory must be new. Use `--modes`
to choose or reorder cases; the default order is the four cases listed above.
Run in isolation. Complete B8 benchmarks remain separate manual runs.

`study.json` records arguments and source/binary hashes, and `source_snapshot/`
preserves the diagnostic sources. Each case saves its
configuration, DRAM environment, logs, `diagnostic_events.jsonl`, timing result,
raw samples, memory mappings, and `profile.json`. `analysis.json` compares the
four timing windows; `summary.csv` provides a compact timing table and
`hotspots.csv` lists sampled functions and estimated CPU seconds in the separate
profiling window. Sampler percentages are exclusive instruction-pointer
samples, not inclusive call-stack times: inline work remains attributed to its
containing function, unresolved samples stay in the denominator, and scheduling
work caused by power accounting appears under scheduler functions. No samples
or lost samples fail the profiled run rather than yielding a misleading report.

Direct inline counters are always enabled for the first diagnostic window,
using exact boundary resets independently of the guest ROI. They are inactive
during warmup and the subsequent profiling window. The dedicated calibration
mode compares timer-on/off windows internally; ordinary study and diagnostic
runs always collect the direct counters.

For matched B8 timer-on/off calibration without statistical sampling:

```bash
python -m experiments.closed_loop_overhead.power_diagnosis \
  --reference-study experiments/closed_loop_overhead/results/b8_overhead_optimized \
  --output experiments/closed_loop_overhead/results/b8_timer_calibration \
  --modes architecture_only logic_and_dram \
  --start-us 1000 --window-us 100 --calibrate-power-timing
```

This uses the existing executable/preload and rebuilt native libraries, runs one
randomized-order on/off pair per mode, and reports observed runtime differences,
direct inline seconds, and call counts in `analysis.json`. One pair is a
calibration with host noise, not a confidence interval or full-workload PASS.

A single window per case is a diagnosis, not a confidence interval. A small
DRAM-only difference may be host noise; repeat with reversed case order or
additional windows before claiming a precise DRAM overhead. Matching boundary
cycles confirms equal simulated duration, not full numerical equivalence.

Test ring decoding and ELF symbol attribution without starting B8:

```bash
python -m unittest experiments.closed_loop_overhead.test_native_profile
```

### Initial B8 diagnosis (historical)

The four-case diagnostic completed on 2026-10-26 used the 1.0–1.1 ms timing
window and a separate 1.1–1.2 ms profiling window, with CPUs 0, 2, 4, and 6.
Its generated `results/b8_power_diagnosis` directory was removed during results
cleanup. The following values are retained as a historical summary, with one
observation per case:

| Enabled accounting | Host wall time | Process CPU time | Wall-time increase |
| --- | ---: | ---: | ---: |
| Architecture only | 16.270 s | 16.230 s | — |
| Logic only | 25.189 s | 25.129 s | 54.82% |
| DRAM only | 16.547 s | 16.507 s | 1.70% |
| Logic and DRAM | 25.502 s | 25.440 s | 56.74% |

All cases reached the same clock boundaries and exited successfully at the
intentional stop. The profiles contained 8,279 samples in total, with no lost
records. The final B8 numerical check was not reached. These window-specific
ratios do not replace the full B8 results, and the small DRAM difference has no
confidence interval.

The main additional hotspot is `vp::ClockEngine::enqueue`: 30.28% of logic-only
samples and 29.07% with both power paths enabled. In the logic-only profile,
751 of its 755 samples land in the linked-list traversal used to insert events.
`PowerTrace::dump_vcd_trace` schedules next-cycle energy-maintenance events even
when waveform output is disabled. The direct power arithmetic is therefore not
the main optimization target; event insertion is. A focused scheduler change
must preserve same-cycle ordering and cancellation behavior. Simply dropping
the maintenance callbacks would also affect expiry of parent energy
contributions and risks changing energy totals.

`vp::Block::pause_all` is another large cost (45.95% of baseline samples), but
its sampled absolute time stays near 7.2–7.5 seconds across the profiles. It
walks the component tree after internal SystemC steps and is a separate
optimization opportunity. This diagnosis left SoftHier sources and installed
binaries unchanged. The former `queue_hotspot.json` and `validation.json`
artifacts recorded instruction-level evidence and bounded checks; they are no
longer available in the workspace.

## One-time capture plus 1-second replay overhead

This additional experiment measures **one fresh SoftHier recording plus a
1-second temperature-aware replay** on the completed study's **67,139-cell
(50 µm) mesh**. It reuses that study's executable, preload, architecture,
electrical configuration, stack, CPU affinity, and optimized native binaries.
It does not build hardware or rerun the four closed-loop cases.

First, inspect the configuration without starting a simulation:

```bash
/home/kzhu/miniconda3/envs/py312/bin/python -m experiments.closed_loop_overhead.replay_overhead plan
```

Run the full experiment manually:

```bash
/home/kzhu/miniconda3/envs/py312/bin/python -m experiments.closed_loop_overhead.replay_overhead run \
  --reference-study experiments/closed_loop_overhead/results/b8_overhead_optimized \
  --prefix b8_replay_50um_1s \
  --max-seconds 1 \
  --replicates 1
```

This runs **one pilot workflow and one measured workflow**: two B8 captures and
two 1-second replays in total. Each workflow creates its own recording. The
capture uses constant-profile logic/DRAM power accounting and the existing
recording hook, with no thermal solver. Its numerical result and architecture
cycle signature must match the completed B8 study. Replay recomputes logic
leakage from temperature; DRAM uses its fixed electrical characterization.
Power updates and thermal steps are both **20 µs**, so a successful replay has
**50,000 steps**, **67,139 cells**, and **one solver setup** (LU factorization
or CG preconditioner setup, inherited from the reference). Reaching the
requested time limit is successful; stopping early at the model limit is not.
The full replay thermal timeline is 1 second; capture is preparation for this
timeline, not an additional thermal prefix.

The main execution-time breakdown uses the same four categories as the figure:

| Category | Capture contribution | Replay contribution |
|---|---|---|
| SoftHier execution | Workload ROI, excluding directly timed inline power routines | Zero |
| Power accounting + capture | Inline accounting and power capture/mapping | Activity lookup/resampling, leakage recomputation, power mapping |
| 3D-ICE thermal steps | Zero | Native source/RHS updates and linear solves |
| Coordination + output | Hook coordination and output inside ROI | Communication, feedback, bookkeeping, and histories inside the replay loop |

The combined bar is **capture ROI + replay loop**, with the capture charged
exactly once. Startup, shutdown, and recording preparation are excluded from
that bar, as initialization/finalization are excluded from the original bars.
They are retained separately, and all categories reconcile with the full
workflow wall time. Recording preparation includes validation and conversion
of the capture trace before replay starts. Build time and post-run analysis are
excluded. Timings include instrumentation overhead without a correction.

Results are saved under `results/b8_replay_50um_1s/`. Each workflow retains
`capture/`, `recording/`, `replay/`, raw timing events, and `measurement.json`.
`analysis.json`, `summary.csv`, and `runs.csv` contain capture, replay, and
combined costs, excluded setup/finalization costs, and host seconds per
simulated second with and without capture. The pilot is excluded. With one
measured workflow there is no confidence interval; additional measured
workflows use independent workflow bootstrap intervals, not pairing with the
older closed-loop study. Normal replay histories and final state are written;
dense maps at every interval are disabled.

To resume, rerun the same command with `--resume`. Completed workflows are
validated and skipped. A failed/interrupted workflow is preserved under a
`*_failed_*` directory and restarted from capture. Settings, source files,
reference inputs, and native artifact hashes must match. The runner shares the
closed-loop study lock to prevent simultaneous benchmark jobs.

If replay startup fails after a successful **pilot** capture, prepare recovery
without repeating that capture:

```bash
python -m experiments.closed_loop_overhead.replay_overhead recover-pilot \
  experiments/closed_loop_overhead/results/b8_replay_50um_1s
```

Then rerun the original `run` command with `--resume`. Preparation runs no
simulation. It rechecks numerical PASS, raw capture timing, recording hashes,
power configuration, reference inputs, and native binaries. Original sources,
measurement, and failed replay outputs are preserved under `recoveries/`;
capture files stay in place. Source changes are limited to the replay/recovery
code and documentation, with unchanged capture setup and command settings.
Recovery is allowed only before any measured workflow starts. Measured
workflows always use fresh captures. The recovered pilot remains excluded from
performance statistics, and its elapsed interruption is labeled
`pilot_recovery_gap`, not recording preparation.

The socket client prevents local source/destination port collisions before
connecting. A confirmed server bind conflict retries on a new port (up to
three attempts); unrelated startup errors still stop the run. Failed startup
logs and native timing are archived separately. The successful thermal session
still has one solver setup; any retries contribute to initialization wall
time and are listed in `replay/summary.json`.

After completion, append the measured fifth bar:

```bash
/home/kzhu/miniconda3/envs/py312/bin/python \
  plot_script/experiment_4_closed_loop_overhead/plot_closed_loop_overhead.py \
  --replay-study experiments/closed_loop_overhead/results/b8_replay_50um_1s
```

The fifth bar is labeled **Replay (1 s)** and includes the one-time architecture
capture plus thermal replay; the closed-loop bars cover 25.6 ms. The replay bar
has no baseline-relative overhead percentage because the simulated
durations differ. This measures repeated recorded activity, not a fresh
1-second architectural simulation. Omitting `--replay-study` keeps the original
four-bar figure. Incomplete studies and short validation runs cannot supply
the fifth bar. With replay included, the figure is 4.5 × 2.15 inches.

Recompute reports without running simulations:

```bash
python -m experiments.closed_loop_overhead.replay_overhead analyze \
  experiments/closed_loop_overhead/results/b8_replay_50um_1s
```

Short validation, independent of the full benchmark:

```bash
python -m unittest experiments.closed_loop_overhead.test_replay_overhead
python -m experiments.closed_loop_overhead.replay_overhead smoke \
  --output experiments/closed_loop_overhead/results/replay_overhead_smoke_new
```

The native check runs a small arithmetic kernel with accounting timing on/off,
then replays each recording for **1 ms / 50 steps** at the actual 67,139-cell
mesh. It requires identical capture power, replay power, energy, and thermal
state. These checks do not run B8 or establish full-workload performance.
