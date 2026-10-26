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
including inline power work and small unclassified instrumentation costs.
Time before/after ROI is initialization/completion. `detail_seconds` retains
nested diagnostic spans; those totals must not be added to the exclusive
phase totals.

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

The completed original `b8_overhead` study is legacy data. It remains readable;
do not overwrite or resume it with changed sources. Run comparisons sequentially.

The compiled hook client and 3D-ICE client/server hashes are recorded alongside
the native simulator hashes. Helper compilation occurs before timed runs.
`state/adapter_diagnostics.json` records worker status, configuration-load count,
rows/bytes read, bounded buffer usage, and worker CPU time. Worker/socket failures
fail the run; an ambiguous exchange is never retried automatically. Interrupted
runs restart with fresh thermal state through the existing study-resume workflow.

`timing.coupling_diagnostics` reports power-publication-to-solver delay and nested
adapter stage times. Nested times and client/worker round trips overlap thermal
work; do not add them to the exclusive phase totals. Inline power accounting is
still included in architecture execution, not fully isolated by `power_capture`.

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

These short checks are not B8 performance results. Their on/off ratios include
host noise and polling effects; report them as a calibration, not a guaranteed
instrumentation overhead bound. Main B8 measurements require numerical PASS,
identical guest cycle counts, matching native/generated mesh counts, one
factorization, and one thermal step per positive-duration power exchange.
