# Four-layout thermal replay: B8 / KV1024 / opt 26

This is the historical B8/OPT-26 report. The current B1/KV4096/OPT-16 campaign
and its launch command are documented in [README.md](README.md).

This follow-up evaluates the existing default, edge inward, balanced and top-row
refinement layouts through **1 second** of thermal replay with
**h = 35,000 W/(m² K)** and **model clamping enabled**. The original h=25,000,
single-execution optimization results remain in `results/final_comparison/`.

## Configuration and provenance

- Workload: `decode_b8_kv1024`, optimization step 26, layer 1, seed 19, one
  measured layer per repeated block, other application defaults retained.
- Four layouts are unchanged from the completed optimization study. Independent
  cluster orientations remain rigid symmetries of `square_bands`.
- Both default leakage profiles: `constant` and `temperature_aware`.
- Logic on DRAM; initial/ambient temperature 300 K; 20 µs power slots and
  integration steps; 16,384 target cells per die; one factorization thread.
- Each of the eight runs advances exactly 50,000 slots to 1 second. Startup runs
  once. Only the measured timer region repeats; the final reporting tail does not.
- Dense native maps preserve every active-die thermal cell at every slot.
  The primary comparison remains the maximum cell temperature over both active
  dies and the entire execution. Component-average peaks are reported separately.
- Above the archived logic leakage table's 125 °C endpoint, the requested clamp
  holds the leakage multiplier at its endpoint. It does not cap temperature.
  DRAM command and background power repeat at their original fixed electrical
  characterization.

The complete constant-profile traces from the four earlier simulations are
imported using the repository's existing thermal replay recorder. It changes
only the copied stack's top heat-transfer coefficient from 25,000 to 35,000;
`recording.json` records the override and input hashes. The copied system
contract retains the source configuration as provenance. The effective cooling
coefficient is verified from each runtime `ice.stk`.

Every recorded dynamic-power sample and reference-leakage/background sample is
identical across layouts. The source ELF/preload hashes, native timer bounds,
stage timings, physical settings and legal transforms are checked before import.
All runs use the same native solver binary as the original study.

The exact native timer bounds are **260,000–6,492,269,000 ps**, giving a
**6.492009 ms** repeated block. This includes 13 ns of timer-region overhead
beyond the 6.491996 ms sum of measured application stages. The replay uses the
absolute native timer bounds, as required by the existing replay workflow.

## Reproduction

The following commands describe the original run, before the campaign defaults
were changed. Use its archived implementation to reproduce that historical
preparation; current default commands run the B1/KV4096/OPT-16 campaign.

```bash
source tmp/bootstrap/env.sh
python experiments/layout_opt/replay_study.py prepare
python experiments/layout_opt/replay_study.py run --workers 4
python experiments/layout_opt/finish_replay.py
```

The historical campaign is `b8_opt26_h35000_replay1s`. Preparation requires a fresh
campaign directory. Pass `--campaign NEW_NAME` to all three commands to create
another campaign. The run controller reuses only completed, validated jobs and
preserves incomplete histories. The finishing controller can run concurrently:
it waits for complete jobs, validates their histories, measures every native
cell, and generates the comparison after all eight jobs finish.

Files are retained under:

- `artifacts/b8_opt26_h35000_replay1s/recordings/`: four portable recordings.
- `artifacts/b8_opt26_h35000_replay1s/runs/`: eight native replay histories.
- `artifacts/b8_opt26_h35000_replay1s/study.json`: settings, sources and completion.
- `artifacts/b8_opt26_h35000_replay1s/logs/`: individual run and analysis logs.
- `results/b8_opt26_h35000_replay1s/`: per-run measurements and validation.
- `results/b8_opt26_h35000_replay1s/final_comparison/`: all four layouts together.

## Results

**All eight runs completed exactly 1 second and passed validation.** Each run
completed 154 measured blocks plus a partial next block. Every native active-die
cell was scanned over all 50,000 slots. Peaks below are the maximum over both
active dies and the complete history; all eight maxima occur on the logic die.

| Layout | Constant peak (°C) | Temperature-aware peak (°C) | Aware DRAM peak (°C) |
| --- | ---: | ---: | ---: |
| Default | 82.423 | 102.781 | 98.856 |
| Edge inward | 81.238 | 101.219 | 97.559 |
| Balanced | 78.623 | 98.178 | 95.784 |
| Top-row refinement | **77.820** | **97.173** | **95.226** |

Top-row refinement is the best of these four saved layouts. It lowers the
temperature-aware peak by **5.608 °C** relative to default, and the constant
peak by **4.603 °C**. The successive temperature-aware improvements are
1.562 °C for edge inward, another 3.041 °C for balanced, and another 1.005 °C
for top-row refinement. The ordering survives the full one-second replay.

The top-row temperature-aware hotspot is cluster 15's RedMulE at **0.99876 s**.
Its final-frame maximum is 95.500 °C, so the final frame alone would understate
the actual peak. The spread between the 16 RedMulE component-average maxima
falls from **10.016 to 1.840 °C**. This spread compares each component's own
maximum over time; it is not a simultaneous spatial spread in one frame.

All eight runs replay exactly the same per-component dynamic activity and DRAM
background power. Dynamic energy is **10.571587426 J** in each run, and
constant-profile leakage/background energy is **1.423453092 J** in each layout.
Thus the constant-profile improvement comes from placement at identical power.
Temperature-aware leakage then responds to the resulting temperatures.
Reducing the hottest location does not necessarily lower total leakage: edge
inward has slightly higher leakage/background energy than default
(5.314265183 versus 5.309540489 J), despite its lower peak. Top-row refinement
reduces that energy to 5.282076615 J.

The leakage clamp was enabled for all eight runs, but **none reached the
125 °C feedback-temperature limit**, so the endpoint clamp was never needed.
The earlier h=25,000 experiment also had a different execution duration; its
absolute temperatures should not be used to isolate the effect of changing h.

## Figures and data

The complete comparison is in
[final_comparison](results/b8_opt26_h35000_replay1s/final_comparison/):

- [Temperature histories](results/b8_opt26_h35000_replay1s/final_comparison/temperature_history.png)
  show all four layouts, both profiles and both dies.
- [Peak comparison](results/b8_opt26_h35000_replay1s/final_comparison/peak_temperatures.png)
  and [layout diagrams](results/b8_opt26_h35000_replay1s/final_comparison/layouts.png).
- [Numerical comparison](results/b8_opt26_h35000_replay1s/final_comparison/comparison.csv),
  [comparison JSON](results/b8_opt26_h35000_replay1s/final_comparison/comparison.json)
  and the small [replay summary](replay_summary.json).

Every GIF and peak map has four panels in optimization order: default, edge
inward, balanced, top-row refinement.

| Profile / die | Full replay GIF | Detail map at each layout's true peak |
| --- | --- | --- |
| Temperature-aware / logic | [GIF](results/b8_opt26_h35000_replay1s/final_comparison/logic_die_temperature_aware_comparison.gif) | [Peak map](results/b8_opt26_h35000_replay1s/final_comparison/logic_die_temperature_aware_own_peaks_detail.png) |
| Temperature-aware / DRAM | [GIF](results/b8_opt26_h35000_replay1s/final_comparison/dram_die_temperature_aware_comparison.gif) | [Peak map](results/b8_opt26_h35000_replay1s/final_comparison/dram_die_temperature_aware_own_peaks_detail.png) |
| Constant / logic | [GIF](results/b8_opt26_h35000_replay1s/final_comparison/logic_die_constant_comparison.gif) | [Peak map](results/b8_opt26_h35000_replay1s/final_comparison/logic_die_constant_own_peaks_detail.png) |
| Constant / DRAM | [GIF](results/b8_opt26_h35000_replay1s/final_comparison/dram_die_constant_comparison.gif) | [Peak map](results/b8_opt26_h35000_replay1s/final_comparison/dram_die_constant_own_peaks_detail.png) |

GIF panels show the same simulation time. They sample every 10 ms, with the
first/final slots and every layout's true peak slot also included. Their fixed
temperature scale is shared across all four layouts and both profiles for each
die. Additional `*_own_peaks_detail` snapshots use a closer, explicitly labeled
scale shared by their four panels, making spatial differences visible after the
whole die has warmed. Each panel labels its own peak time. Full-scale peak maps,
matched final-time maps and PDF versions are also retained.

## Validation

Preparation checks pass for all four inputs, including rigid-transform legality
and unchanged workload/timing. All eight completed histories pass **268**
temperature, power and energy consistency checks. Their per-component dynamic
activity and DRAM background hashes match exactly. Effective runtime cooling
is verified as 35,000 W/(m² K), with 20 µs steps and slots, 300 K initialization,
and 50,000 slots per run. The dense scans cover 16,736 logic cells and 16,384
DRAM cells in every slot.

The cooling-override, two-die model-clamp and logic-only temperature-factor tests
pass. The streaming native-cell analyzer reproduces both active-die peaks and
hotspot positions from the earlier default run exactly; its indexed frame reads
match the complete native histories. Per-run `validation.csv` files and native
map hashes are retained alongside each measurement.

Final output checks confirm that the solver and archived replay sources are
unchanged, all comparison inputs match, and every frame of all four GIFs
decodes. Each constant-profile GIF has 105 frames; each temperature-aware GIF
has 103. All include every compared layout's true peak and the final slot.
Temperature histories and peak-detail maps were also inspected for readable
labels, correct panel order and shared scales. See
[verification.json](results/b8_opt26_h35000_replay1s/final_comparison/verification.json).
