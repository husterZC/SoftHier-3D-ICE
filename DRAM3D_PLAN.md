# SoftHier memory on logic thermal co-simulation

Implement a memory-on-logic variant of the existing provider → system contract →
3D-ICE workflow, with an `experiments/llm_dram3d/` study using the SDK's complete
transformer-block application. Preserve the existing single-die workflow.

## Agreed scope

- SoftHier baseline: `a453fa09fe53ff306c91072066ea3ff559a0853a`, branch
  `chi/power_interface`.
- SDK baseline: `b454aeee4278bb00ecbfcdcc4d1366fe8451d7c8`, branch
  `chi/soft_hier_dram3d`.
- One DRAM die with 16 channel regions for the SDK's 4 × 4 architecture.
  Each channel occupies exactly the rectangle of its corresponding cluster.
- Logic and DRAM active faces face one another across a bonding layer.
- Obtain DRAM channel power from DRAMPower. Exclude controller and PHY power.
- Physical and electrical parameters are configurable inputs. The user authorized
  reasonable defaults; shipped electrical values are clearly labeled engineering
  estimates, not measured HBM2 characterization. Archive resolved inputs in runs.
- Keep all work, dependencies, artifacts, and temporary files inside this repo.

## Starting evidence

The old exporter accepts the new architecture but exports only 304 logic power
domains and 320 logic floorplan regions. The new configuration needs 320 power
domains and 336 floorplan regions after adding 16 DRAM channels.

The baseline DRAMSys wrapper exposes no cumulative energy interface to GVSoC.
The pinned DRAMSys dependency uses DRAMPower 5.4.1, which has no HBM2 backend;
the supplied HBM2 configuration also lacks electrical power parameters. Enabling
`PowerAnalysis` alone therefore does not implement DRAM heat generation.

The pinned 3D-ICE source supports multiple powered dies and bulk/source layers.
Stack descriptions are written top to bottom, while power input is consumed by
powered source layers bottom to top.

## Implementation sequence

### 1. Dependency pins and reproducible native additions

- [x] Update the SoftHier gitlink and initialize its pinned dependencies.
- [x] Update the provider SDK branch and exact commit; update provenance/docs.
- [x] Track additional native sources/patches and apply them to provider-managed
      staged sources; record baseline commits and extension hashes.
- [x] Validate native ABI/capabilities in build-cache checks.

### 2. DRAMPower energy reporting

- [x] Implement HBM2 core-energy support from explicit characterization inputs.
- [x] Expose cumulative command, refresh, and background energy per instance.
- [x] Sample at GVSoC power-hook boundaries, including idle and final intervals.
- [x] Preserve units (DRAMPower joules, GVSoC picojoules), channel ownership,
      untimed-preload semantics, and monotonic/idempotent cumulative snapshots.
- [x] Exclude controller and PHY/interface energy.
- [x] Validate controlled command traces before claiming meaningful DRAM heat.

### 3. Contract and face-to-face geometry

- [x] Add a multi-die contract version while continuing to accept version 1.
- [x] Export architecture-selected DRAM channels and explicit die assignments.
- [x] Generate separate logic/DRAM floorplans and a thermal artifact manifest.
- [x] Validate the complete stack, source orientation, materials, dimensions,
      power ownership, and exact channel/cluster alignment.

Bottom-to-top package: existing PCB layers → package bumps → logic bulk → logic
source → F2F bond → DRAM source → DRAM bulk → TIM → copper → top cooling boundary.
Each total die thickness includes its active/source sublayer. Existing package
defaults remain 4 × 250 µm silicon PCB, 50 µm bumps, 4 × 7.5 µm TIM, 50 µm copper,
and top convection of 15,000 W/(m²·K) at the configured initial temperature.

### 4. Coupling and run management

- [x] Generalize generated inputs, runtime copying, hook configuration, status,
      termination, and rendering to the declared die manifest.
- [x] Send one power vector in native bottom-to-top order: logic then DRAM.
- [x] Wait for the same slot from both Tflp outputs, using slot indices rather
      than rounded microsecond timestamps.
- [x] Return each component's own die temperature. Keep DRAM voltage rails
      separate from logic voltage initialization.
- [x] Preserve legacy command-line behavior, output names, and final-slot policy;
      report exact power-window and thermal-slot durations distinctly.

### 5. LLM DRAM3D experiment

- [x] Add preparation, paired study runner, analysis, regression tests, and README.
- [x] Reuse SDK generation and its 11-stage `LLM_DRAM3D_PASS` validation;
      compile `layer.elf` and use the combined `preload.elf`.
- [x] Stage private native HBM2 configurations without the old HBM4 mapping edit.
- [x] Start with `decode_b1_kv1024`, then a batch-8 case; expose all SDK cases.
- [x] Default to a 20 µs coupling interval and archive all model/input hashes.
- [x] Report logic and DRAM power/energy and render both dies separately.

The constant/temperature-aware comparison initially changes logic leakage only.
Temperature-dependent DRAM power requires separate characterized input data;
fixed DRAM parameters must not be described as temperature-aware DRAM physics.

### 6. Validation and compatibility

- [x] Contract/geometry checks, missing-input rejection, and channel mapping tests.
- [x] Native idle/refresh/read/write/channel-isolation/energy-conservation tests.
- [x] Two-die zero-power equilibrium and independent logic/DRAM heating checks.
- [x] Full hook init/update/final lifecycle and correct sentinel/vector widths.
- [x] SDK numerical pass; paired workload/provenance consistency checks exercised
      with synthetic study fixtures and a real coupled channel smoke pair.
- [x] Existing interface tests, default single-die run, and llm_decode regressions.
- [x] Small memory-on-logic smoke case; full-size LLM studies remain explicit runs.
- [x] Existing replay recordings remain supported; reject unsupported multi-die
      replay explicitly until that workflow is extended.

## Explicit experiment inputs

`--thermal-config` supplies DRAM total thickness, logic/DRAM source thicknesses,
DRAM thermal conductivity and volumetric heat capacity, and bond thickness,
conductivity and volumetric heat capacity. `--dram-power-config` supplies the
electrical model: voltage rails, command/background/refresh characterization,
reference temperature, units, per-channel/pseudo-channel scope, and provenance.
Resolved values are validated and archived before large preload generation.

The user subsequently authorized reasonable numerical defaults. The provider
ships documented thermal and electrical parameter files; experiments validate
and archive those estimates, and accept replacements through the same interface.

## Work log

- 2026-10-14: Plan accepted and saved. Baseline sources inspected under ignored
  `tmp/dram3d_plan/`. Implementation started in three independent areas: native
  DRAM power, geometry/contract, and experiment adaptation; root integration
  covers provider pins, runtime coupling, documentation, and validation.

- 2026-10-14: User authorized reasonable defaults. Added explicit thermal and
  electrical JSON profiles with engineering-estimate provenance and overrides.
- 2026-10-14: Native DRAMSys tests passed for open/closed pages, both pseudo-
  channels, masked writes, all-bank/per-bank refresh, low-power modes, untimed
  preload, idle/final sampling and independent instances. Native two-die solver
  tests passed equilibrium and independent heating with cross-die conduction.
- 2026-10-14: First integrated 16-channel smoke passed 81,920 data checks,
  320 sampled power domains and 9 synchronized thermal slots. Actual power
  duration was 170.549 µs; thermal duration was 180 µs because the final
  10.549 µs power interval uses one full 20 µs thermal slot.
- 2026-10-14: All 11 SDK capacity plans passed. Batch-1 KV1024 generated a
  260,502,336-byte combined preload and compiled its real `layer.elf`.
- 2026-10-14: Batch-1 KV1024 passed 36,296 SDK numerical checks across all
  11 stages, totaling 10,439,993 cycles. All 17 direct loaders completed at cycle
  zero. Evidence: `tmp/dram3d_dev/llm_b1_uncoupled/validation.json` and
  `simulation.log`. The provider shell exited with a parsing error after the
  simulation because it had been edited while running; this is recorded in the
  validation JSON separately from the SDK PASS. The study runner freezes its
  provider script before launch. This numerical run used an earlier native
  reporting build; the final library was validated by the coupled cases below.
- 2026-10-14: A full-size geometry smoke exposed unstable parallel native
  factorization: positive power produced temperatures below ambient. Replaying
  the identical trace with one solver thread restored physically valid output.
  Version 2 stacks now use one thread; version 1 is unchanged. Added a full 4 × 4
  geometry regression and a physical lower-bound check for thermal outputs.
- 2026-10-14: Final native bundle
  `fe0aacd38a7bf749fa1fb229f32fbbd4e637dbf3a97090f6e07f2411a93acfc9`
  passed coupled 16-active-channel constant/temperature-aware runs, a single
  active channel with 15 quiet channels, and a legacy single-die run. All native
  HBM2 counters agree with integrated hook energy. The paired runs have identical
  power windows, logic dynamic power and DRAM power; logic leakage follows the
  selected profile. Evidence is under `runs/dram3d_validation/` in
  `smoke16_stable_constant/validation.json`,
  `smoke16_stable_temperature_aware/validation.json`,
  `smoke1_stable/validation.json`, `logic_only/validation.json`, and
  `stable_pair_validation.json`.
- 2026-10-14: All 90 interface tests pass, including native multi-die thermal
  checks and existing replay integration. Legacy `llm_decode` regressions pass.
  Build stamps validate native capabilities, recursive source-module identities,
  SystemC's actual installation and binary hashes. The pinned upstream submodules
  have no tracked source edits.
- 2026-10-14: Rebuilt the simulator with the final cache checks and reran the
  single-active-channel coupled case. `smoke1_final_cache/validation.json`
  reproduces the previous stable numerical, energy and temperature results
  exactly. Separate logic and DRAM animations were rendered from the stable
  temperature-aware run.
- 2026-10-14: All 13 LLM experiment tests and 5 smoke-validator tests pass.
  Production analysis now checks the physical temperature lower bound for
  component feedback, floorplan temperatures and every raw thermal-map cell.
  It accepts both stable profiles and rejects the original unstable trace.
  Native-source archives also include the provider's Python build helpers and
  detect changes to those helpers after workload preparation.

## Full LLM experiment execution

The implementation and end-to-end validation are complete. The full coupled
`decode_b1_kv1024` constant/temperature-aware pair has passed SDK numerical
validation. Other LLM cases, including batch 8, have not been run. Use the commands
in `experiments/llm_dram3d/README.md` for those studies. The
shipped defaults are uncalibrated engineering assumptions; the results above
validate numerical execution, power accounting and coupling rather than a
particular manufactured DRAM device.

- 2026-10-14: At the user's request, removed the standalone smoke-test source,
  preparation/validation scripts and their dedicated tests from
  `experiments/llm_dram3d/smoke/`. Kept production thermal validation and the
  experiment regression suite. Started the complete `decode_b1_kv1024`
  constant/temperature-aware study with the documented model defaults.
- 2026-10-14: Both full coupled profiles passed all 36,296 SDK checks and all
  11 stage counters, with identical 10,439,993-cycle layer execution and zero
  preload cycles. Each run completed 579 synchronized thermal slots over
  11,561.363 µs of actual power-window time. Study manifest:
  `experiments/llm_dram3d/logs/b1_kv1024_20261014/study.json`.
- 2026-10-14: Full paired analysis passed source/input hashes, exact stage timing,
  power inventories, logic leakage equations and physical bounds for every
  thermal cell. Logic dynamic and DRAM power match exactly across profiles.
  Peak cell temperatures are 31.297/31.265 °C (logic/DRAM) for constant leakage
  and 31.379/31.347 °C for temperature-aware leakage. Logic energy is
  19.580954/20.599136 mJ respectively; DRAM energy is 7.592469 mJ in both runs.
  Wrote the report, five CSV tables, PNG/PDF comparisons and eight 84-frame GIFs
  under `experiments/llm_dram3d/results/b1_kv1024_20261014/`.
