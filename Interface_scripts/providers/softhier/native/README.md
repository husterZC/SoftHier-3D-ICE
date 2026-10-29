# SoftHier HBM2 core power extension

The pinned DRAMPower 5.4.1 release has no HBM2 backend. This provider adds
`DRAMPower::SoftHierHBM2`, a core-only backend using DRAMPower's command dispatch
and energy interfaces. It does not substitute another DRAM standard. DRAMSys
continues to schedule and check the HBM2 command stream. These files are applied
to staged sources, leaving the pinned upstream submodules intact.

The initial profile in `../configs/dram3d_power.json` contains **uncalibrated
engineering estimates**, authorized as starting values for this experiment.
It is not a vendor specification or measured characterization. All reported
energy and temperature results inherit this limitation. Replace the profile
with characterized electrical values for quantitative device conclusions.

## Electrical model

`SOFTHIER_DRAM_POWER_CONFIG` names a JSON file validated by `power_config.py`.
Each rail supplies voltage in V, state current in A, and incremental command
charge in C. The state currents apply to one **complete pseudo-channel**,
including its devices and banks; there is no additional device/stack multiplier.
DRAMSys constructs one backend for each physical channel. Its HBM2 rank index
identifies the pseudo-channel. The exported instance energy sums physical
channels and pseudo-channels once.

The model evaluates:

```
background_energy_J = sum_rails(voltage_V * current_A[state] * duration_s)
command_energy_J = sum_rails(voltage_V * incremental_charge_C[command])
```

The background states are all banks precharged, at least one bank active,
active power down, precharged power down, and self refresh. An active state's
current represents the whole pseudo-channel, rather than a per-active-bank
increment. ACT, PRE, read, write, all-bank refresh, and per-bank refresh have
explicit command charges. PREA charges PRE for each bank actually closed.
Masked writes consume a full core write burst. Read/write auto-precharge adds
PRE at the HBM2 timing boundary: maximum of tRAS and read recovery or write
latency + burst duration + write recovery. An explicit precharge cancels a
pending auto-precharge for that bank activation.

Command charges **exclude the background energy already integrated over the
operation**. They are attributed at command issue (auto-precharge at its own
scheduled time); a refresh charge therefore includes its incremental operation
energy but not its concurrent state background. Self-refresh current includes
internal refresh activity; the backend does not synthesize extra refresh
commands. Low-power entry and exit have no separate transient charge in schema
version 1. Interface, controller, PHY, termination, and data-toggle energy are
excluded. The characterization temperature is recorded but does not change
currents with thermal feedback. A temperature-dependent electrical model would
need additional characterization and a new schema.

Untimed ELF/memory preloads use the existing direct storage API and never enter
the DRAMPower command stream. Future timed commands are queued so their energy
cannot appear before their issue timestamp. Snapshot calls include idle time,
are cumulative, permit repeated identical timestamps, and reject backwards
time. Unsupported command types fail explicitly.

## Build and transport

Run these commands with paths inside the repository:

```
python3 Interface_scripts/providers/softhier/native/apply_native.py --softhier STAGED_SOFTHIER
python3 Interface_scripts/providers/softhier/native/apply_native.py --dramsys STAGED_DRAMSYS
python3 Interface_scripts/providers/softhier/native/apply_native.py --fingerprint
```

The DRAMSys stage must first have SoftHier's pinned shared-library patch applied.
Build it with `DRAMSYS_USE_DRAMPOWER=ON` (the upstream wrapper Makefile's
`DRAMSYS_WITH_DRAMPOWER` spelling is not the CMake option). Set `PowerAnalysis`
true and `EnableWindowing` false in the private DRAMSys simulation configuration.
The external power hook owns the sampling windows.

`patch_manifest.json` records exact input/output source hashes. The applicator
rejects unexpected or partially patched sources, verifies its postconditions,
and writes `.softhier_native_energy.json` with source revisions and bundle hash.
Its fingerprint covers the patches, manifest, applicator, power-model headers,
and timing headers. A rebuild must occur when this fingerprint changes.

The C ABI is:

```c
int softhier_dram_energy_abi_version(void); /* returns 1 */
int dram_get_core_energy_j(int instance_id, uint64_t time_ps,
                           double *command_j, double *background_j);
/* 0: success; 1: no HBM2 core backend; -1: invalid request/model error */
```

Both energy outputs are cumulative joules. The GVSoC wrapper converts them to
picojoules and registers a generic `BlockPower` external-energy callback. The
callback is included in `get_total_energy()` for the component and recursively
for its ancestors, with no change to the hook JSON protocol. The thermal hook
therefore samples background energy even when a channel receives no requests
and includes the final partial interval. Existing trace-only GVSoC power reports
do not include this separate external callback; hook output is the experiment's
authoritative power record.

The existing JSON `dynamic_w` field carries command energy per interval;
`leakage_w` carries state background energy per interval for DRAM. This is an
accounting category and does not imply that all DRAM background consumption is
transistor leakage. Thermal source power is their sum. Only `dram3d_chan_*`
wrappers register this callback; ordinary off-chip memory settings remain
independent.

## Clock-event insertion

The staged engine caches the last insertion position for one absolute clock
cycle. Repeated insertions at that cycle append after its existing events
without rescanning the delayed queue. Other deadlines use the original search.
Cancellation, dequeue before callback execution, reset, and event deletion
invalidate the cache. Event ordering, wake-up calculations, and power callbacks
are preserved. The patch changes the clock-engine class layout, so rebuild the
engine, models, and launchers together with `provider.sh build-hardware`.

The cache is part of the normal provider build. Transport
`COUPLING_MODE=legacy` does not select the original scheduler.

## Direct inline-power timing

The overhead experiment enables `CLO_POWER_ACCOUNTING_TIMING=1` together with
`CLO_TIMING_DIR`. Ordinary provider runs leave this optional probe disabled.
`power_accounting_timing.h` declares the engine-owned accumulator and scopes;
its implementation is compiled once through the staged `power_trace.cpp`.
DRAMSys receives enter/leave callbacks through
`softhier_dram_set_power_timing_callbacks` (ABI version 1), avoiding independent
counters in each shared library. Nested logic/DRAM work is counted once.

Only workload ROI time outside hook exchanges is collected. Power-source checks,
trace accounting, maintenance callbacks and their enqueue calls, and the DRAMPower
command-processing block are covered. Generic scheduler work outside those
routines is not attributed to power. The single simulation-thread restriction
is enforced; accumulated wall times cannot silently overlap. One scalar JSONL
record reports elapsed nanoseconds and outer-call count at ROI end. Disabled
probes perform no clock reads or logging. The diagnostic launcher can select an
external bounded window with `CLO_POWER_TIMING_EXTERNAL_WINDOW=1`.

Native timing tests load a separate shared library and verify nesting, paused
hook work, reset/disabled behavior, and invalid concurrent calls with a controlled
clock. Full integration checks and bounded calibration are documented in
`experiments/closed_loop_overhead/README.md`.

## Verification

`tests/hbm2_core_test.cpp` checks hand-computed idle, active, refresh, read/write,
auto-precharge, power-down and self-refresh energies, fractional final windows,
future-command ordering, instance isolation, and interval conservation.
`tests/test_power_config.py` checks malformed/ambiguous profiles are rejected.
These synthetic arithmetic cases validate accounting, not the electrical
accuracy of the default estimates. A full native build and controlled DRAMSys
trace run are additionally required before an LLM thermal result is accepted.

The native integration runner exercises open and closed pages, all-bank and
per-bank refresh, and staggered low-power management using the real shared
library. It checks both pseudo-channels (including a masked write), two isolated
DRAMSys instances, idle/background energy, untimed preload exclusion, and a
final half-cycle snapshot:

```
python3 -m unittest discover -s Interface_scripts/providers/softhier/native/tests -p 'test_*.py'
python3 Interface_scripts/providers/softhier/native/tests/run_native_tests.py \
  --dependencies SoftHier/.power_interface/dependencies \
  --dramsys-build DRAMSYS_BUILD_DIRECTORY \
  --output tmp/native_energy_validation
```

The runner uses only repository-local source, libraries, outputs, and compiler
temporary files. It does not download or build its dependencies.
