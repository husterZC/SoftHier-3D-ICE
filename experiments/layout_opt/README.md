# Cluster layout optimization

The current campaign uses **batch-1 decode, KV=4096, OPT step 16 (SW, Striped
Weights), h=25,000 W/(m² K)**. It evaluates the four saved layouts: default,
edge inward, balanced and top-row refinement. Each layout is replayed through
**1 second**, with **model clamping enabled**, under both constant and
temperature-aware logic leakage.

The earlier B8/KV1024/OPT-26 results are historical: [REPORT.md](REPORT.md)
contains the single-execution optimization and [REPLAY.md](REPLAY.md) contains
its h=35,000 replay. The saved layouts are candidates for the new workload;
their old ranking is not assumed to remain valid.

## OPT aliases

| Step | Alias | Name |
| ---: | --- | --- |
| 0 | OM | Owner Mapping |
| 9 | DO | DMA Overlap |
| 13 | DW | Distributing Workers |
| 16 | SW | Striped Weights |
| 17 | LC | Local Channels |

`experiments/llm_dram3d/run_study.py` and `prepare_workload.py` accept these
case-insensitive aliases with `--opt-step`, as well as all existing numeric
steps 0–26. For example, `--opt-step SW` and `--opt-step 16` select the same
SDK code. Manifests preserve the numeric step and add `opt_alias` and `opt_name`.
Current layout campaign defaults are in [config.py](config.py).

## Launch the parallel campaign

From the repository root, after pushing the changes:

```bash
source tmp/bootstrap/env.sh
python experiments/layout_opt/run_parallel.py --workers 4 --require-pushed --detach
```

The launcher prints its controller PID and progress/log paths. `--detach`
keeps it running in the background after the terminal or agent turn ends.
`--require-pushed` checks that HEAD matches the tracked upstream before work
starts. Omit `--detach` to run in the foreground. Use `--build-hardware` only
when the native backend needs building; this finishes before concurrent jobs.

The script performs these dependent phases automatically:

1. Prepare and compile one shared B1/KV4096/SW workload.
2. Run four isolated layout trials concurrently, each with both leakage profiles.
   Each trial has a separate solver port, output directory and log.
3. Validate completed application runs and compare their activity and geometry.
4. Import the new constant-profile traces into portable replay recordings.
5. Run the eight one-second replays with up to four workers.
6. Validate every native thermal-cell history and render comparisons of all four
   layouts, including both dies, both profiles, peak maps and synchronized GIFs.

Builds and manifest writes have one owner. A file lock prevents duplicate
controllers. Failed jobs stop dependent phases and are recorded with log paths;
raw partial histories are preserved. Completed jobs may be reused only when
settings, source hashes and layouts still match. An incomplete simulation is
not silently overwritten or treated as a valid result.

## Progress and results

Progress is refreshed every **15 seconds** by default, including during workload
preparation, simulation and rendering:

- Human-readable file:
  `experiments/layout_opt/artifacts/b1_kv4096_opt16_h25000/progress.md`
- Machine-readable file:
  `experiments/layout_opt/artifacts/b1_kv4096_opt16_h25000/progress.json`
- Main log:
  `experiments/layout_opt/artifacts/b1_kv4096_opt16_h25000/controller.log`
- Per-phase and per-layout logs:
  `experiments/layout_opt/artifacts/b1_kv4096_opt16_h25000/pipeline_logs/`
- Application capture results:
  `experiments/layout_opt/results/b1_kv4096_opt16_h25000/`
- Final one-second comparison:
  `experiments/layout_opt/results/b1_kv4096_opt16_h25000_replay1s/final_comparison/`

For a quick terminal view:

```bash
cat experiments/layout_opt/artifacts/b1_kv4096_opt16_h25000/progress.md
```

The JSON includes the phase, current Git commit, controller/worker PIDs, job
exit codes, simulated capture time, replay seconds/slots, component-temperature
previews, and analysis progress. Only the completed native-cell analysis defines
the final peak-temperature result. Use `--campaign NEW_NAME` for a fresh run;
the replay campaign defaults to `NEW_NAME_replay1s`.

## Fixed physical and numerical conditions

Each cluster retains its original square footprint and position. Its complete
`square_bands` partition uses one of eight rigid symmetries: R0, R90, R180,
R270, MX, MY, MD or MA. Component identities, areas and power mappings remain
unchanged. DRAM placement and package materials are unchanged. The exact
candidate definitions are in [layouts](layouts/).

Other defaults remain: logic on DRAM, initial/ambient temperature 300 K, layer 1,
seed 19, one measured layer per replay block, 20 µs power/integration intervals,
16,384 target cells per die, and the default electrical models. Startup runs
once; only the measured timer region repeats. Every replay advances 50,000
slots. Model clamping holds the leakage multiplier at the 125 °C table endpoint
if needed; it does not cap temperature.

The objective is the maximum native thermal-cell temperature over both active
dies and every simulated slot, primarily for temperature-aware leakage. Dynamic
activity, source inputs, timing, cooling and legal cluster transforms must match
before layout results can be compared. All figures obtain workload/OPT/cooling
labels from the actual study manifest.

## Validation and optimization history

```bash
source tmp/bootstrap/env.sh
python -m unittest experiments.layout_opt.test_campaign \
  experiments.layout_opt.test_layout_opt experiments.layout_opt.test_analysis -v
```

[JOURNAL.md](JOURNAL.md) records layout hypotheses, observations and campaign
changes. [search.py](search.py) and [refine.py](refine.py) retain the original
proposal tools; this campaign evaluates the four existing candidates with fresh
activity. Its selected winner is the best measured candidate, without a global
optimum claim.

Raw workloads, native outputs, source snapshots and analyses stay under
`artifacts/` and `results/`, which are excluded from Git. All experiment paths
must stay inside this repository. The older `parallel_profile.py` is retained
as historical orchestration; new campaigns use `run_parallel.py`.
