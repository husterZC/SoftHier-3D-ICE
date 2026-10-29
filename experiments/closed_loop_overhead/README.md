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
counts, factorization, source/RHS/solve times, ROI markers, and process exits.

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

The layout is 3.6 × 2.85 inches for a single journal column. Muted solid fills,
light gridlines, and a shared label for the three closed-loop grids keep the
breakdown readable at that size. Bar labels show total minutes in bold and
relative overhead underneath. Fill colors and the style version are recorded
in the JSON metadata; all segment heights remain proportional to measured time.

The figure shows workload execution time in minutes, excluding initialization
and finalization. Percentages use the architecture-only workload baseline.
The legend contains four workload phases: SoftHier execution (blue), power
estimation (yellow), 3D-ICE thermal steps (orange), and coordination/output
(green). Yellow combines directly measured inline accounting with capture and
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
