# Layout optimization: decode B8 / KV1024 / opt step 26

For the later **1-second replay at h=35,000 with model clamping enabled**, see
[REPLAY.md](REPLAY.md). This report covers the original h=25,000 optimization.

**The best tested layout is [03_top_refinement](layouts/03_top_refinement.json).**
Independent cluster orientations reduce the temperature-aware peak from
**43.928 to 40.369 °C**, a **3.559 °C** reduction (20.84% of the rise above the
26.85 °C ambient). All eight full application runs have completed and passed
the experiment's validation checks.

## Measured results

Peak means the maximum native thermal-cell temperature across both active dies
and every slot of the complete execution. It is not a component average or the
temperature in the final animation frame.

| Layout | Constant peak (°C) | Temperature-aware peak (°C) | Aware RedMulE component peak spread (°C) |
| --- | ---: | ---: | ---: |
| Default, all R0 | 43.624 | 43.928 | 6.285 |
| Edge inward | 42.996 | 43.278 | 5.751 |
| Balanced spatial search | 40.690 | 40.938 | 1.745 |
| **Top-row refinement** | **40.138** | **40.369** | **1.142** |

RedMulE spread is the difference between the largest and smallest individual
RedMulE component-average maxima over the execution. It is distinct from a
simultaneous spatial spread in one frame. The selected layout also lowers the
temperature-aware DRAM peak from **35.771 to 33.729 °C**. Its remaining logic
hotspot is cluster 8's RedMulE at 6.46 ms.

All four layouts have exactly matching dynamic power at every component
and time window, identical ELF/preload hashes, and identical stage timing:
**6,491,996 cycles / 6.491996 ms per layer**. Thus the measured difference is due
to the changed placement and its thermal/leakage feedback. Dynamic energy is
68.6392 mJ in all eight runs. Constant-profile total leakage/background energy
also matches exactly at 9.25969 mJ. The peak-temperature improvement therefore
does not depend on reducing the application's work or its constant-profile power.

## Fixed experiment

`decode_b8_kv1024`, opt step 26, heat-transfer coefficient 25,000 W/(m² K),
**no thermal replay**. All other `llm_dram3d` defaults remain: logic on DRAM,
one layer execution, layer 1, seed 19, 20 µs power/integration step, 16,384
target cells per die, and both default leakage profiles. Actual active-die
meshes have 16,736 logic cells and 16,384 DRAM cells. Each run starts at 300 K.
The default optional final numerical check remains disabled; completion,
stage timing, source/input hashes, activity and thermal-physics checks pass.

Every cluster remains in its original square footprint. Every component in a
cluster receives the same rigid rotation/reflection of `square_bands`; areas,
identities, power/feedback mappings, DRAM placement and stack settings remain
unchanged. The experiment uses an isolated provider wrapper; shared floorplan
rules and existing experiments are unchanged.

## What explains the original asymmetry?

There are two effects. In the default layout, cluster 0's RedMulE touches the
left and bottom chip edges. Cluster 15's has clearance from the corresponding
outer edges. Their lateral heat-spreading conditions differ.

The MoE work is also close to balanced rather than exactly equal. Its 90 gate/up
output tiles and 45 down tiles are distributed round-robin across 16 clusters:
clusters 0–9 receive 9 tiles, 10–12 receive 8, and 13–15 receive 7. Full-phase
RedMulE dynamic power is 0.60230 W in cluster 0 versus 0.46856 W in cluster 15.
The measured ratio, 1.28544, agrees with 9/7 = 1.28571. Normalizing power by tile
count leaves a coefficient of variation of only 0.036%. MoE occupies 89.91% of
the layer time.

The layout comparisons preserve this unequal workload exactly, isolating the
placement benefit without changing the application schedule.

## Optimization decisions

1. **Move edge RedMulEs inward.** This removes cluster 0's two edge contacts,
   but makes its RedMulE touch those in clusters 1 and 4. The hotspot moves to
   cluster 5; the final aware improvement is only 0.650 °C.
2. **Balance boundary clearance and hot-block spacing.** A static spatial
   heuristic proposes orientations using measured power, three distance scales,
   and 24 deterministic search starts. The selected layout keeps approximately
   184 and 395 µm gaps from cluster 0 to its two RedMulE neighbors while adding
   395 µm bottom clearance. Full simulations validate the 2.990 °C improvement.
3. **Refine the new upper-edge hotspot.** The balanced control's maximum moves
   to cluster 13. Enumerate all 4,096 top-row assignments with the lower three
   rows fixed, using measured per-cluster peaks to calibrate static score ratios.
   The selected top row is R90 / R180 / MA / R90. It leaves cluster 13's own
   orientation unchanged while changing its neighbors. Full simulations reduce
   the aware peak by another **0.569 °C**, to **40.369 °C**. The new maximum occurs
   near clusters 8 and 12 rather than at the former upper-edge hotspot.

## Selected transforms

Coordinates start at the bottom left; cluster index is `x + 4*y`. The table is
drawn from the top row to the bottom row. R90/R180/R270 are counterclockwise
rotations; MY reflects y, MD exchanges x/y, and MA reflects across the opposite
diagonal. [README.md](README.md) defines the exact coordinate formulas.

| y | x=0 | x=1 | x=2 | x=3 |
| --- | --- | --- | --- | --- |
| 3 | c12: R90 | c13: R180 | c14: MA | c15: R90 |
| 2 | c8: R180 | c9: R180 | c10: R180 | c11: MY |
| 1 | c4: R180 | c5: R180 | c6: R180 | c7: R180 |
| 0 | c0: MY | c1: MY | c2: R270 | c3: MD |

To reproduce the selected layout with the prepared workload, use a fresh trial
name:

```bash
source tmp/bootstrap/env.sh
python experiments/layout_opt/run.py run \
  --name reproduce_top_refinement \
  --layout experiments/layout_opt/layouts/03_top_refinement.json
```

The static scores only propose layouts; they do not replay activity or integrate
temperatures. This is a bounded heuristic search, not a global-optimum proof
over all 8^16 layouts. Results apply to the requested single-layer transient and
the existing electrical/thermal models; physical wire delays are not remodeled.

## Evidence and reproduction

- [Decision journal, including failed attempts](JOURNAL.md)
- [Exact candidate layouts](layouts/)
- [Small recorded summary](summary.json)
- [Validated comparison data](results/final_comparison/comparison.json)
- [Layout diagrams](results/final_comparison/layouts.png)
- [Temperature histories](results/final_comparison/temperature_history.png)
- [All four steps: native logic-cell GIF, fixed shared color scale](results/final_comparison/logic_die_comparison.gif)
- [All four steps: native DRAM-cell GIF, fixed shared color scale](results/final_comparison/dram_die_comparison.gif)
- [Logic maps at each layout's own peak](results/final_comparison/logic_die_own_peaks.png)
- [DRAM maps at each layout's own peak](results/final_comparison/dram_die_own_peaks.png)
- [RedMulE temperatures and MoE power](results/final_comparison/redmule_temperature_and_work.png)
- [Commands and validation workflow](README.md)

Thermal maps and GIFs show all four steps in a 2×2 grid: default and edge inward
on the top row, balanced and top-row refinement on the bottom row. All panels
share a fixed temperature scale per die. GIFs use synchronized times and include
every layout's true peak slot and the final slot. The `*_baseline_peak.png` maps
show all four layouts at the default layout's peak time, which need not be the
other layouts' peak time. The separate `*_own_peaks.png` / `.pdf` maps show each
layout at its own maximum and label its time explicitly.
The native maps retain 0.001 K output precision; no mesh or timestep tuning was
used to obtain a lower score. Thirteen targeted tests cover legal symmetries,
native parsing and comparison guards. The refinement enumeration also passes
checks for unique assignments, fixed lower rows and correct minimum selection.

Raw runs, archived inputs, sources, logs and thermal maps are retained under
`artifacts/b8_opt26_h25000/trials/`. Generated results are local artifacts excluded
from Git. The candidate profiles use the documented concurrent-controller
handoff; native completion and validated `study.json` entries establish success.
