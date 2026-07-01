# SoftHier + 3D-ICE Co-Simulation Tutorial

This repository provides a root-level flow for running SoftHier and 3D-ICE
together. The intended user path is:

```bash
make bootstrap
make coupled-run RUN_NAME=default_app
```

For a single first-time command, use:

```bash
make co-simulation RUN_NAME=default_app
```

`make co-simulation` runs `bootstrap` first and then launches the coupled simulation.

## What The Flow Does

The co-simulation flow:

1. initializes SoftHier and its nested submodules;
2. applies/verifies the SoftHier GVSOC/PULP patches;
3. verifies the runtime power-capture hook needed by the coupled flow;
4. builds or verifies the required 3D-ICE binaries;
5. generates 3D-ICE geometry, floorplan, and stack files from the selected
   SoftHier architecture using the root `Interface_scripts/geometry_generator`
   tools;
6. builds SoftHier with the selected runtime power trace path;
7. starts 3D-ICE server mode;
8. starts a root-owned adapter that follows SoftHier power rows and writes
   complete 3D-ICE power slots;
9. in the default `DICE_RUN_MODE=local-server` mode, has the 3D-ICE server read
   those power slots directly from `traces/3dice_power_traces.txt`;
10. runs SoftHier;
11. has the 3D-ICE server write stack-declared thermal outputs after each slot;
12. sends the all-`-1` termination slot when SoftHier exits;
13. optionally generates an end-of-run temperature dashboard GIF when
    `ICE_GENERATE_GIF=1`;
14. writes per-run metadata and summary files.

## Host Dependencies

The bootstrap script checks for the required commands before doing work. On
Debian/Ubuntu, the usual package set is:

```bash
sudo apt-get install build-essential cmake bison flex libopenblas-dev csh unzip git python3 python3-venv python3-pip
```

SoftHier may also download or prepare its own toolchain and SystemC tree during
`source SoftHier/sourceme.sh`. That work is done by SoftHier's existing setup
logic.

## First-Time Setup

From the repository root:

```bash
make bootstrap
```

This target:

- initializes `SoftHier`;
- initializes nested SoftHier submodules;
- applies SoftHier's own patches through `make -C SoftHier drmasys_apply_patch`;
- verifies the runtime power hook from `SoftHier/soft_hier/gvsoc_core.patch`;
- runs SoftHier's environment preparation;
- initializes the `3D-ICE` submodule;
- builds or verifies `3D-ICE/bin/3D-ICE-Server`;
- builds or verifies `3D-ICE/bin/3D-ICE-Client` for the optional socket mode.

If 3D-ICE is not initialized yet, `Interface_scripts/co-simulation/3dice_client_server.sh`
initializes the `3D-ICE` submodule and builds in place:

```text
3D-ICE/
```

The installation path is inside the `3D-ICE` submodule.

## Running The Default Coupled Simulation

After bootstrap:

```bash
make coupled-run RUN_NAME=default_app
```

The run directory is timestamped by default:

```text
runs/default_app/YYYYMMDD-HHMMSS/
```

The latest run for each `RUN_NAME` is also linked at:

```text
runs/default_app/latest
```

While SoftHier is running, the coupled runner redraws a green framed live window
with the latest 5 SoftHier log lines. The full output is still stored in:

```text
runs/default_app/latest/logs/softhier.log
```

Change or disable the live view with:

```bash
make coupled-run RUN_NAME=default_app SOFTHIER_LOG_TAIL_LINES=10
make coupled-run RUN_NAME=default_app SOFTHIER_LOG_TAIL_LINES=0
```

## Choosing The 3D-ICE Run Mode

The default mode is local server-only mode:

```bash
make coupled-run RUN_NAME=default_app DICE_RUN_MODE=local-server
```

In this mode, the runner starts no 3D-ICE client. The server command is shaped
like this inside the run-local 3D-ICE result directory:

```bash
3D-ICE-Server ice.stk --power-trace trace.txt --follow --until-minus-one
```

The server follows the adapter output file, consumes one complete power row per
thermal slot, writes the stack-declared output files itself, and exits after the
adapter appends an all-`-1` termination row.


## One-Command First Run

If you want the setup and run in one command:

```bash
make co-simulation RUN_NAME=default_app
```

This is the closest "push a button" path. It may still take a long time on a
fresh machine because SoftHier and 3D-ICE have real build dependencies.

## Run Directory Layout

Each run is self-contained:

```text
runs/<run-name>/<timestamp>/
  run.env
  summary.txt
  generated/
    geo.json
    3dice/
      floorplan_nopower.flp
      ice.stk
      conductance_layer.txt
  traces/
    softhier_power_raw.txt
    3dice_power_traces.txt
  results/
    3dice/
      floorplan_nopower.flp
      ice.stk
      conductance_layer.txt
      output_top_die.txt
      xyaxis_TOP_DIE.txt
      thermal_map.txt
      temperature_map.gif   # only when ICE_GENERATE_GIF=1
  logs/
    3dice_server.log
    3dice_client.log        # only present in DICE_RUN_MODE=client-server
    adapter.log
    softhier.log
    tmap_gif.log            # only when ICE_GENERATE_GIF=1
  pids/
  state/
    softhier.done
```

Important files:

- `run.env`: exact configuration, paths, ports, and git commits for the run.
  It also records `ICE_TARGET_TOP_DIE_CELLS`, `EFFECTIVE_ICE_SLOT_SECONDS`,
  `EFFECTIVE_ICE_STEP_SECONDS`, and the optional `ICE_GENERATE_GIF` settings.
- `summary.txt`: process statuses, trace row counts, thermal row count, and max
  temperature.
- `traces/softhier_power_raw.txt`: SoftHier raw runtime power rows.
- `traces/3dice_power_traces.txt`: adapter output consumed directly by the
  3D-ICE server in local-server mode, or by the 3D-ICE client in client-server
  mode.
- `results/3dice/output_top_die.txt`: full TOP_DIE `Tmap` temperatures
  written directly by the 3D-ICE server.
- `results/3dice/xyaxis_TOP_DIE.txt`: non-uniform Tmap cell geometry used to
  interpret `output_top_die.txt`.

The current generated stack emits a `Tmap`
output. For SSH/headless runs, render the
latest full Tmap slot as a self-contained HTML dashboard:

```bash
python Interface_scripts/plot_runtime_temperature_map/plot_runtime_tmap.py \
  --coords runs/<run-name>/latest/results/3dice/xyaxis_TOP_DIE.txt \
  --map runs/<run-name>/latest/results/3dice/output_top_die.txt \
  --html runs/<run-name>/latest/results/3dice/temperature_map.html \
  --follow \
  --poll 2
```


To keep the runtime HTML smooth, GIF export is offline/end-of-run. Enable it on
the coupled command with:

```bash
make coupled-run RUN_NAME=default_app ICE_GENERATE_GIF=1
```

The default GIF path is
`runs/<run-name>/latest/results/3dice/temperature_map.gif`.

You can also generate the GIF manually after any run:

```bash
python Interface_scripts/plot_runtime_temperature_map/plot_runtime_tmap.py \
  --coords runs/<run-name>/latest/results/3dice/xyaxis_TOP_DIE.txt \
  --map runs/<run-name>/latest/results/3dice/output_top_die.txt \
  --gif runs/<run-name>/latest/results/3dice/temperature_map.gif \
  --once
```


## Running Other Config + App Pairs

Example: SoftHier `arch_test.py` with the `test` application. This app is short,
so use a smaller power interval to get enough thermal slots:

```bash
make coupled-run RUN_NAME=arch_test_app \
  CFG=$PWD/SoftHier/examples/SoftHier/config/arch_test.py \
  APP=$PWD/SoftHier/examples/SoftHier/software/test \
  PWR_INTERVAL_PS=100000
```

Example: NoC512 config with the GEMM systolic app:

```bash
make coupled-run RUN_NAME=noc512_gemm \
  CFG=$PWD/SoftHier/examples/SoftHier/config/arch_NoC512.py \
  APP=$PWD/SoftHier/examples/SoftHier/software/gemm_systolic
```

To reuse a fixed run directory instead of timestamping:

```bash
make coupled-run RUN_DIR=$PWD/runs/manual/debug_default
```

`BUILD_SOFTHIER=0` is only safe when the existing SoftHier binary was built
with the same `RAW_POWER_TRACE` path. The power trace destination is compiled
into the simulator. With timestamped run directories, keep `BUILD_SOFTHIER=1`
unless you intentionally reuse the exact same `RUN_DIR`.

## Useful Targets

Bootstrap and run:

```bash
make bootstrap
make coupled-run RUN_NAME=default_app
make co-simulation RUN_NAME=default_app
```

Inspect or stop the latest run for a run name:

```bash
make latest-run RUN_NAME=default_app
make coupled-status RUN_NAME=default_app
make coupled-stop RUN_NAME=default_app
```

List run directories:

```bash
make list-runs
```

Cleanup:

```bash
make clean-latest RUN_NAME=default_app
make clean-run RUN_DIR=$PWD/runs/default_app/20260622-101530
make clean-runs
```

`clean-latest` removes the target of `runs/<run-name>/latest` and the symlink.
`clean-runs` removes the full root `runs/` tree.

## Troubleshooting

### 3D-ICE binaries are missing

Run:

```bash
make 3dice-build
```

If dependencies are missing, install the host packages listed above and rerun:

```bash
make bootstrap
```

### 3D-ICE waits forever for slot 0

Check whether SoftHier is emitting raw power rows:

```bash
tail -n 20 runs/<run-name>/latest/logs/softhier.log
wc -l runs/<run-name>/latest/traces/softhier_power_raw.txt
```

Then verify the runtime power hook:

```bash
make softhier-power-check
```

If the hook is missing, rerun:

```bash
make bootstrap
```

### A run is stuck or was interrupted

Stop recorded processes:

```bash
make coupled-stop RUN_NAME=<run-name>
```

Then inspect logs under:

```text
runs/<run-name>/latest/logs/
```

### Need more or fewer thermal slots

Adjust the SoftHier power capture interval:

```bash
make coupled-run RUN_NAME=my_run PWR_INTERVAL_PS=100000000
```

The value is in picoseconds. Smaller values produce more SoftHier power rows
and more 3D-ICE thermal slots.

The generated `.stk` file uses the same slot duration by default:

```text
3D-ICE slot seconds = PWR_INTERVAL_PS * 1e-12
3D-ICE transient step seconds = slot seconds / 10
```

This alignment matters because SoftHier appends one power row per
`PWR_INTERVAL_PS`; the 3D-ICE server consumes one appended line as one thermal
slot in local-server mode. In client-server mode, the 3D-ICE client consumes
that line and forwards it to the server. In both modes, the server writes the
corresponding thermal outputs.

Override these only when you intentionally want a different 3D-ICE time base:

```bash
make coupled-run RUN_NAME=my_run \
  PWR_INTERVAL_PS=100000000 \
  ICE_SLOT_SECONDS=0.0001 \
  ICE_STEP_SECONDS=0.00001
```

The generated floorplan targets about `256*256` non-uniform TOP_DIE cells by
default. To request a different total, set `ICE_TARGET_TOP_DIE_CELLS`:

```bash
make coupled-run RUN_NAME=my_run ICE_TARGET_TOP_DIE_CELLS=65536
```
