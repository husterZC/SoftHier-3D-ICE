# Layout optimization decision log

This records hypotheses, evidence, decisions and measured outcomes. A hypothesis
is not a result. All accepted candidates require fresh closed-loop simulations;
thermal replay is disabled.

## 0 — Establish a controlled baseline

Requested case: `decode_b8_kv1024`, optimization step 26, cooling coefficient
25,000 W/(m² K). Keep the experiment defaults: logic on DRAM, square bands,
one layer repetition, layer 1, seed 19, 20 µs interval/timestep, 16,384 target
cells per die, and both constant and temperature-aware logic leakage profiles.

Primary score: the maximum native thermal-cell temperature across both dies
and the complete temperature-aware execution. Also report the constant-profile
score, RedMulE component temperatures, spatial spread, timing and energy.

No prior baseline artifacts exist in this checkout. Reproduce the baseline here
instead of importing results from another workspace.

Hypothesis: a RedMulE near an insulated chip edge has less lateral spreading
area than an otherwise similar RedMulE facing the interior. The common local
bottom-left position puts different physical boundary conditions around corner
clusters. Unequal instantaneous activity remains a competing explanation.
Measure per-cluster activity/power and temperatures before attributing the
asymmetry to placement.

Allowed changes: apply one of the eight square symmetries independently to
each entire cluster. Preserve cluster position/size, each component's area and
identity, power/feedback mapping, DRAM alignment, workload and thermal stack.

Planned search: first test a boundary-informed placement, then use its measured
result and a cheap spatial interaction score to choose a more balanced pattern.
The spatial score will only propose candidates; full co-simulation decides the
winner. It is not a thermal replay or a claim of a global optimum over 8^16
layouts.

## 1 — Boundary-informed candidate (before complete baseline)

The first baseline samples reproduce the reported direction: at roughly 1.04 ms,
cluster 0's RedMulE component averages 34.028 °C versus 31.462 °C in cluster 15.
These are partial-run observations, not final peak scores. Save the sampled
power averages and temperatures in `results/early_baseline.json`.

In each 753.56 µm square, RedMulE occupies a 569.54 × 358.99 µm rectangle at
the local bottom-left corner. Its short distance to the bottom/left chip edges
makes a boundary effect plausible. Early-stage work can also heat neighboring
units differently; final MoE power statistics will check the workload premise.

Candidate `01_edge_inward` mirrors x in left-edge clusters and y in bottom-edge
clusters (both mirrors are a 180° rotation at the bottom-left corner). Other
clusters keep R0. This tests a simple, interpretable change. Counter-hypothesis:
it can bring hot blocks closer across cluster boundaries and move the peak
inward rather than lower it.

Independent baseline/candidate simulations may run concurrently after the
single shared hardware build, with separate run directories and orchestration
ports. This only saves host time; simulated work and physical settings are
unchanged. Host runtimes are not used as an optimization score.

The first `01_edge_inward` attempt failed before GVSoC started: reflection
produced a −6.04e−14 µm coordinate, which the native parser rejects. Preserve
that attempt. The separate native export adapter snaps only tiny negative
boundary roundoff to zero; areas and dimensions are unchanged. A native zero-
power test with all eight symmetries now passes at 300 K. The actual candidate
run is `01_edge_inward_v2`. R0 geometry is bit-for-bit unchanged, so the already
running baseline continues with its original archived implementation.

At the saved 1.74 ms prefix, RedMulE mean dynamic power is 0.507 W in cluster 0
and 0.404 W in cluster 15. Thus the early corner temperature difference cannot
be attributed entirely to layout. Final MoE-specific statistics remain needed.

## 2 — Balance heat spreading and hot-neighbor crowding

Use two static load summaries from completed baseline windows: mean power over
the measured prefix and the last 400 µs. Save the exact cutoff and source data
in `results/search_02/static_loads.json`. This does not evolve temperatures or
repeat any activity and is not a thermal replay.

The proposal score sums softened, exponentially decaying distance weights from
component heat sources, including reflected sources at chip boundaries. Three
length scales (250, 500, 1,000 µm) avoid choosing a layout for one arbitrary
spreading length. Sample every component center and nine RedMulE locations.
The fixed DRAM sources remain aligned with their original clusters.

Search all eight choices in each coordinate update, using 24 deterministic
starts (seed 19). Minimize the worst normalized score over both load summaries
and all three scales. The simple inward candidate's worst score is 0.996 of
baseline; `02_balanced_search` scores 0.866. These dimensionless scores are
neither temperature predictions nor measured improvement percentages. The
score does not solve the layer stack or model transient leakage feedback.

Decision: test `02_balanced_search` with the full application and both default
leakage profiles. Its rationale is to retain boundary clearance while avoiding
unnecessarily close groups of large hot RedMulE rectangles. Record every
accepted search move in `results/search_02/search.json` and assess the result
against the complete baseline, including timing and activity equality.

First matched native observation (`results/matched_prefix_01.json`): through
1.06 ms / 53 slots in the constant-leakage profile, peak cell temperatures are
35.551 °C (baseline), 35.453 °C (inward) and 34.073 °C (balanced). Every dynamic
power value matches exactly over these windows. These are incomplete executions,
so they do not establish the final ranking. They support the decision to test
both boundary clearance and hot-neighbor spacing instead of using edge distance
alone. Continue all three full paired trials before selecting a layout.

## 3 — Check the workload-balance premise

The fixed application uses N=64 output tiles. Each expert gate/up projection
has 5,760 outputs (90 tiles); each down projection has 2,880 outputs (45 tiles).
At step 26, B8 shares each expert across all 16 clusters using
`j = cid; j < ceil(n/64); j += 16`. Consequently clusters 0–9 receive 6+3=9
output tiles per expert batch, clusters 10–12 receive 5+3=8, and clusters 13–15
receive 5+2=7. Both projections use K=2,880, so the difference also affects the
number of RedMulE multiply calls. This is a source-derived work count, not an
assumption of equal per-tile wall time. The source is archived under the shared
workload's `source/sw/main.c`; dimensions are in `decode_b8_kv1024/layer.h`.

This gives a second explanation for the baseline corner asymmetry: cluster 0
receives about 29% more MoE output tiles than cluster 15. A 400 µs sample ending
at 5.24 ms shows the expected tiers (about 0.598 / 0.525 / 0.451 W dynamic),
although full phase averages are still required. Placement optimization keeps
this mapping fixed and favors giving the heavier-loaded clusters more thermal
spreading room.

The second matched prefix (`results/matched_prefix_02.json`, 2.16 ms / 108 slots)
has exactly matching dynamic power and peak-so-far temperatures of 38.587 °C
(baseline), 38.427 °C (inward), and 36.494 °C (balanced). This isolates a real
placement effect despite the unequal work distribution. Continue to use full-
run temperature-aware peak temperature for final selection.

## 4 — Complete the independent leakage controls concurrently

To reduce elapsed host time, start the two candidates' temperature-aware profiles
while their constant profiles are still running. Each profile uses its own
output directory and socket port; all use the same prebuilt hardware, workload,
20 µs step and 16,384-cell target. No native simulation is interrupted and no
thermal activity is replayed.

`parallel_profile.py` owns the candidates' aware directories. The original
sequential controllers will archive their completed constant profiles, then
stop at the existing-directory guard. That expected controller exception is
not a native simulation failure. The new controllers merge the completed
constant and aware entries only after both archives exist. Their source and
mode are recorded under each trial's `orchestration/` directory. The baseline
uses its original sequential controller.

The complete baseline constant run validates successfully: 6,491,996 layer
cycles (6.491996 ms), of which 5,836,865 cycles are MoE. Its peak native cell is
43.624 °C at 6.46 ms, in cluster 0's RedMulE at approximately (11.865, 11.965) µm.
The DRAM peak is 35.627 °C. Full MoE RedMulE mean dynamic power spans
0.46856–0.60230 W (9.32% coefficient of variation), consistent with the 7–9
tile distribution. The full-run measurement is saved in
`results/00_baseline/constant_only.json`; this is still only the control profile.

Re-evaluate the three fixed candidates' static scores using the complete
baseline power trace (`results/full_load_check/`). The balanced candidate's
worst score ratio remains 0.865, versus 0.994 for inward and 1.000 for default.
Thus the original proposal remains favored when the early prefix is replaced
with the complete load summary. This check does not simulate temperatures and
does not establish the final winner.

The exported geometry explains the weak inward result. At baseline, cluster 0's
RedMulE touches the left and bottom chip edges, but is separated from cluster 1's
RedMulE by 184.02 µm and cluster 4's by 394.57 µm. Moving it inward in candidate 1
removes both edge contacts but makes those two inter-RedMulE gaps **zero**.
Candidate 2 uses MY in cluster 0 and coordinates neighboring orientations:
bottom clearance becomes 394.57 µm while the two original gaps remain. Its left
edge contact remains, so this is a tradeoff between boundary exposure and
neighbor heating rather than maximizing edge distance. Exact distances are
saved in `results/redmule_spacing.json` and the layouts in
`results/layout_preview/layouts.png`. These geometric facts support the observed
prefix ranking; they do not isolate every contribution in the layered thermal
model.

The full-phase dynamic-power ratio between clusters 0 and 15 is 1.28544,
matching the 9/7 tile ratio (1.28571). Dividing each cluster's mean dynamic power
by its assigned tile count reduces the coefficient of variation to 0.036%.
`results/00_baseline/moe_tile_balance.csv` records the counts, powers and
component temperatures. This supports the workload-imbalance explanation
without changing the application's schedule.

## 5 — Refine the hotspot that remains after balancing

Both candidate controls now validate. Inward reaches 42.996 °C (0.628 °C below
default), but the hotspot moves to cluster 5 near the adjoining hot-block group.
Balanced reaches 40.690 °C (2.934 °C below default); RedMulE component peak spread
falls from 6.162 to 1.716 °C. The remaining balanced hotspot is in cluster 13,
near the top chip edge, at 6.36 ms. Clusters 12 and 14 are also relatively hot.

Decision: test one final refinement driven by this measured hotspot. Keep
clusters 0–11 fixed and enumerate all 8^4=4,096 top-row assignments. For every
cluster, anchor the six static spatial scores to its measured balanced-control
peak rise above ambient. Rank alternatives by the worst inferred relative rise
over all clusters and all six load/length-scale scenarios. Including clusters
below the top row penalizes moving heat into an already warm neighbor. This is
a local proposal heuristic; the one-layout calibration does not establish
temperature-prediction accuracy and does not replay or integrate temperatures.

`refine.py` selects top-row transforms [R90, R180, MA, R90] for clusters 12–15;
all lower rows retain candidate 2's transforms. It leaves cluster 13 itself
unchanged, but separates its RedMulE from cluster 14's and reorients the outer
top-row neighbors. The calibrated score ratio is 0.946, which is not a measured
temperature reduction. Store every evaluated assignment in
`results/search_03/search.json` and validate `03_top_refinement` using two fresh,
full co-simulations. Its independent profiles run concurrently with separate
ports and output directories, using the same documented controller handoff.

At the matched 1.30 ms aware prefix, refinement is slightly warmer than balanced
(34.731 versus 34.702 °C peak so far), with exactly equal dynamic power. Preserve
this observation in `results/matched_refinement_prefix_01.json`. The refinement
targets the later upper-edge hotspot; this early comparison neither confirms nor
rejects its full-run objective. Selection must wait for its complete peak.

While the refinement continues, the first three full pairs finish and validate.
Their temperature-aware peaks are 43.928 °C (default), 43.278 °C (inward) and
40.938 °C (balanced). Balanced improves the requested objective by 2.990 °C,
or 17.51% of the rise above ambient, and reduces RedMulE component peak spread
from 6.285 to 1.745 °C. Its DRAM peak is 34.171 °C. Every dynamic-power value
matches exactly across these six runs; stage cycles, inputs, physical settings,
mesh counts and legal transforms pass the comparison checks. The corresponding
plots and GIFs are retained in `results/comparison/` as the three-layout checkpoint.

Re-running the refinement enumeration from the completed `metrics.json` instead
of the earlier constant-only export reproduces the candidate JSON byte for byte.
Record that check in `artifacts/refinement_reproduction.log`. The final selection
still awaits both complete refinement profiles.

## 6 — Final validation and selection

Both refinement profiles complete and pass all input, source, timing, activity,
geometry and native-temperature checks. Select `03_top_refinement`, the best
of the four fully evaluated layouts:

| Layout | Constant peak (°C) | Temperature-aware peak (°C) |
| --- | ---: | ---: |
| Default | 43.624 | 43.928 |
| Edge inward | 42.996 | 43.278 |
| Balanced | 40.690 | 40.938 |
| Top-row refinement | **40.138** | **40.369** |

The final aware improvement is **3.559 °C** relative to default, or **20.84%**
of the rise above 26.85 °C ambient. Refining the upper row contributes another
0.569 °C beyond the balanced candidate. The remaining hotspot is cluster 8's
RedMulE, near the altered cluster-8/12 adjacency, at 6.46 ms. This confirms the
tradeoff considered during refinement: moving/separating the upper hot blocks
can warm a lower neighbor, but the resulting maximum is lower.

RedMulE component peak spread decreases from 6.285 to 1.142 °C; the aware DRAM
peak decreases from 35.771 to 33.729 °C. Every dynamic-power entry matches
exactly across all eight application runs. Layer time remains 6.491996 ms,
dynamic energy remains 68.6392 mJ, and constant-profile total leakage/background
energy remains 9.25969 mJ. No workload, clock, area, cooling or resolution
tradeoff was used.

The final full comparison is in `results/final_comparison/`; the smaller
`summary.json` and `REPORT.md` preserve the measured outcome alongside the code.
Plots and GIFs use identical scales for the compared layouts and include true
peak slots. The three-layout checkpoint remains in `results/comparison/`.
This completes the bounded search; the selected layout is the best measured
candidate, with no claim of a global optimum over all independent symmetries.

## 7 — Show every optimization step in the final thermal comparisons

The initial final-comparison thermal maps and GIFs selected only the default
and best layout, although the layout diagrams, temperature curves and numerical
tables already included all four. This hid the intermediate spatial changes.

Update `compare.py` to render every supplied layout in a grid, in optimization
order. The final comparison now uses four panels: default, edge inward,
balanced and top-row refinement. All panels share a fixed color scale per die
and the GIFs show the same simulation time in every panel. Sampling includes
all four true peak slots, including the balanced logic peak at 6.36 ms.

Retain matched-time images at the default peak and final slot, and add
`*_own_peaks.png` / `.pdf` with each layout's own maximum and its time labeled.
The latter makes the measured improvement visible even when maxima occur at
different times. Record panel order, color limits and frame slots in
`thermal_maps.json`; check each rendered peak against the existing measurement.
Regenerate these figures directly from the completed native thermal outputs.
The simulations, workload, optimization choices and measured scores stay the same.

Validation: all five comparison/search tests pass, the complete-run workload
checks pass again, and every frame of both 82-frame GIFs decodes successfully.
All eight rendered die/layout peak temperatures match their measurements.
Inspect the static peak maps and a GIF peak frame for readable labels and all
four panels. Record these checks in
`results/final_comparison/visualization_validation.json` and update the plotting
source hash; the native binary and other recorded source hashes are unchanged.

## 8 — One-second replay at h=35,000 with the model clamp enabled

The user requests a separate thermal replay comparison of all four saved layouts
through 1 second, retaining B8/KV1024 and opt step 26, changing the cooling
coefficient to 35,000 W/(m² K), and enabling the model clamp. Preserve the
original single-execution optimization and its results.

Reuse the existing complete constant-profile hook traces. Validate identical
activity, inputs, timing and legal geometry, then import four portable recordings
using the existing replay recorder's cooling override. This changes only the
copied top heat sink. Start each replay from 300 K and retain 20 µs sampling and
integration, the original mesh, stack and electrical models. Run both default
leakage profiles, giving eight independent simulations. Enable dense native maps
to retain the same all-cell, all-time peak objective as the original study.

The first preparation attempt incorrectly compared the timer-region duration to
the sum of application stage cycles. The exact measured native timer bounds are
260,000–6,492,269,000 ps; their 6.492009 ms duration includes 13 ns of timer
overhead beyond the 6.491996 ms stage sum. The importer now checks equal native
timer bounds across the four sources and uses those absolute bounds directly.
The rejected attempt created no recording or simulation; its source snapshot is
retained as `artifacts/b8_opt26_h35000_replay1s_preparation_attempt1/`.

Preparation and three existing cooling/model-clamp tests pass. The new streaming
cell analyzer reproduces the prior default run's exact cell peaks and positions
on both dies. A short isolated transport timing probe finds that native slot
advancement dominates runtime; socket exchange overhead is small, so no solver
or transport settings are changed. Four simulations run concurrently, with
measurement and four-panel comparison generation queued behind completed runs.
See [REPLAY.md](REPLAY.md) for commands and the follow-up's final results.

All four temperature-aware replays reach exactly 1 second, complete 154 measured
blocks after startup, and pass the native-cell and component-history checks.
Their all-cell peaks are 102.781, 101.219, 98.178 and 97.173 °C in optimization
order. Top-row refinement improves the peak by 5.608 °C, and its hotspot is now
cluster 15 at 0.99876 s. None reaches the logic leakage-table limit. Dynamic
energy is exactly matched at 10.571587426 J after saved-history integration.

Render a completed temperature-aware checkpoint while the constant controls
continue. Full-history GIFs keep a fixed shared scale. Additional peak-detail
maps use a shared scale covering the four peak snapshots, since the entire die
is warm by the end of this longer run. Face-colored cell borders remove plotting
seams caused by rounded native coordinates without changing sampled values.
Both 103-frame GIFs decode fully and include every layout's actual peak slot.

## 9 — Complete the paired one-second replay comparison

All eight native replays finish at exactly 1 second, with 50,000 thermal slots
and 154 completed measured blocks plus the final partial block. All 268 saved
temperature, power and energy consistency checks pass. Scan every native cell
in every active-die slot; the resulting peaks are:

| Layout | Constant peak (°C) | Temperature-aware peak (°C) |
| --- | ---: | ---: |
| Default | 82.423 | 102.781 |
| Edge inward | 81.238 | 101.219 |
| Balanced | 78.623 | 98.178 |
| Top-row refinement | **77.820** | **97.173** |

The ordering remains unchanged at the requested replay conditions. Edge inward
reduces the aware peak by 1.562 °C, balanced removes another 3.041 °C, and
top-row refinement removes another 1.005 °C. Overall, top-row improves the aware
peak by 5.608 °C and the constant peak by 4.603 °C. Its aware RedMulE component
peak spread is 1.840 °C, compared with default's 10.016 °C.

The constant runs have identical dynamic and reference-leakage power, which
confirms a placement benefit without temperature-dependent power changes.
The aware runs retain identical dynamic activity and DRAM background power;
logic leakage responds to temperature. Edge inward slightly increases total
leakage/background energy despite lowering the maximum temperature. This is a
useful distinction: redistributing the hottest region need not cool every
component or minimize total leakage. Top-row reduces both the maximum and
total leakage/background energy relative to default.

All eight runs keep the requested clamp enabled, and none reaches the 125 °C
feedback limit. The top-row aware hotspot is cluster 15 at 0.99876 s, whereas
its constant-profile hotspot is cluster 8 at 0.91438 s. Keep peak snapshots
separate from matched final-time snapshots so these differences are visible.
Record the complete paired results in [REPLAY.md](REPLAY.md) and
[replay_summary.json](replay_summary.json), with figures under
`results/b8_opt26_h35000_replay1s/final_comparison/`.

Final figure verification decodes every frame of all four GIFs: 105 frames for
each constant-profile die and 103 for each temperature-aware die. All four
layout peaks and the final slot are included, with synchronized panels and a
shared full-history scale per die. Inspect the temperature histories and both
profiles' peak-detail maps for labels, ordering and spatial detail. The solver,
archived replay sources and analysis hashes match. Save the check results in
`final_comparison/verification.json` and the small replay summary.

The output verifier initially required exact equality for a display timestamp
computed in floating-point seconds. Replace that assertion with a 1 ps
tolerance after separately checking the exact integer endpoint of
1,000,000,000,000 ps. No simulation, measurement or figure data changes are
needed; the completed verification passes.

## 10 — Switch to B1 / KV4096 / SW and automate the parallel campaign

The new request selects batch-1 decode with KV=4096, OPT step **16**, and
25,000 W/(m² K) cooling. Step 16 supersedes the immediately preceding request
for step 17 before any new workload or simulation is started. The user confirms
keeping the one-second replay and `--allow-model-clamp`.

Introduce the requested names: 0=OM (Owner Mapping), 9=DO (DMA Overlap),
13=DW (Distributing Workers), 16=SW (Striped Weights), 17=LC (Local Channels).
The generic LLM preparation and study CLIs accept either a numeric step or its
case-insensitive alias. Archive the numeric step, alias and expanded name.

Evaluate the same four legal orientation candidates with freshly captured
activity for this dataflow. Do not reuse B8/OPT-26 power traces or assume that
its winning layout remains best. Keep earlier results in their original paths.
Read workload, OPT and cooling labels from the actual study manifest when
rendering the new comparisons.

Add `run_parallel.py`: prepare and compile once, run isolated layout captures
with distinct solver ports, validate/measure them, import new recordings, run
the eight one-second replays with four workers, then validate and render every
layout together. A campaign lock gives progress and manifest writes one owner.
Record child PIDs, exit codes, log paths, capture/replay simulation time and
analysis state in atomic `progress.json` and `progress.md` updates every 15 s.
Failures stop dependent phases and retain their raw histories.

The source campaign is `b1_kv4096_opt16_h25000`; its replay campaign is
`b1_kv4096_opt16_h25000_replay1s`. The current request requires an SSH push
before starting the real campaign. Verify SSH remote access, run the relevant
checks, commit/push the implementation, then launch with `--require-pushed`.
Completion and any runtime errors are recorded by the background controller;
this entry makes no temperature-result claim for the new workload.

Pre-launch validation passes: 19 layout/campaign tests and 21 LLM study tests,
including actual parallel-job overlap, queued-job suppression after failure,
exclusive campaign ownership, alias selection, wrong-cooling/clamp rejection,
and the existing rigid-transform/provider checks. Syntax, documentation links
and the resolved B1/KV4096/OPT-16/h=25,000 defaults also pass inspection.
