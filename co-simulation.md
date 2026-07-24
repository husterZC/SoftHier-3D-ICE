# SoftHier + 3D-ICE Closed-Loop Co-Simulation

This repository runs SoftHier and 3D-ICE as a synchronous power/temperature
feedback loop. SoftHier is integrated through a provider boundary; the generic
geometry and 3D-ICE code does not import SoftHier.

## Quick Start

From the repository root:

```bash
make bootstrap
make coupled-run RUN_NAME=default_app
```

The one-command equivalent is:

```bash
make co-simulation RUN_NAME=default_app
```

For a short validation workload:

```bash
make coupled-run \
  RUN_NAME=power_interface_smoke \
  SIMULATOR_CONFIG=$PWD/SoftHier/soft_hier_sdk/examples/SoftHier/config/arch_test.py \
  SIMULATOR_APP=$PWD/SoftHier/soft_hier_sdk/examples/SoftHier/software/test \
  POWER_INTERVAL_PS=10000000 \
  ICE_TARGET_TOP_DIE_CELLS=256 \
  SIMULATOR_LOG_TAIL_LINES=0
```

The smoke interval is 10 µs. It is deliberately larger than a high-resolution
production interval because version 1 starts the Python hook executable once
per exchange.

## Separation of Responsibilities

The architecture side owns:

- physical geometry and floorplan-element definitions;
- exact GVSoC component paths to sample;
- mappings from component power to floorplan power;
- mappings from floorplan temperatures back to components; and
- Kelvin-to-Celsius conversion.

The GVSoC engine owns:

- periodic average power sampling from cumulative energy counters;
- the direct executable invocation;
- versioned request/response validation;
- recursive temperature application; and
- component-temperature readout.

The engine never calls geometry code or links to 3D-ICE.

## What a Run Does

The normal flow is:

1. The selected provider validates SoftHier, the engine hook, and the pinned
   SDK.
2. The provider exports `3dice-cosim-system` version 1 JSON.
3. Generic tools validate the contract and generate geometry, floorplan, and
   stack files.
4. The stack requests `Tflp` average temperatures for the top die.
5. The provider builds the selected SoftHier architecture and workload.
6. The runner starts 3D-ICE in follow mode.
7. GVSoC invokes the hook at time zero. The architecture contract supplies
   exact component paths and initial Celsius temperatures.
8. After every complete interval, GVSoC sends average dynamic, leakage, and
   total subtree power in watts.
9. The hook maps those values into floorplan order and appends one 3D-ICE power
   slot.
10. 3D-ICE solves the slot and appends floorplan temperatures in Kelvin.
11. The hook area-weights the declared floorplan elements, converts to Celsius,
    and returns `{path, temperature_c}` values.
12. GVSoC recursively applies each temperature to the selected component and
    all descendants.
13. At shutdown, GVSoC sends the remaining partial interval as `final`. The
    result is recorded but not applied.
14. The hook appends an all-`-1` power slot so 3D-ICE exits cleanly.

The executable protocol is `gvsoc-power-hook` version 1. See
[`Interface_scripts/README.md`](Interface_scripts/README.md) for the full
contract and protocol boundaries.

## Setup and Reproducibility

`make bootstrap` asks the provider to initialize nested SoftHier submodules,
clone the SoftHier SDK when absent, pin it to:

```text
1244fdbc34977aff5a6a10ead079053fb5d31d00
```

and build or verify the 3D-ICE client/server binaries.

The default SDK URL is SSH-based. A mirror can be selected with:

```bash
SOFTHIER_SDK_URL=/path/to/softhier-sdk.git make bootstrap
```

If a compatible SDK toolchain already exists, avoid another download with:

```bash
SOFTHIER_SDK_TOOLCHAIN_SOURCE=/path/to/toolchain make bootstrap
```

Provider build products and `ccache` data live under
`SoftHier/.power_interface/`. The SDK checkout is
`SoftHier/soft_hier_sdk/`. Both are ignored by the SoftHier integration
branch.

Typical host packages include:

```bash
sudo apt-get install build-essential cmake bison flex libopenblas-dev csh unzip git python3 python3-venv python3-pip
```

## Important Run Controls

The most useful overrides are:

| Variable | Meaning |
|---|---|
| `RUN_NAME` | Name used under `runs/` and for the `latest` link. |
| `RUN_DIR` | Exact output directory; overrides timestamped layout. |
| `SIMULATOR_PROVIDER` | Provider executable. Defaults to SoftHier. |
| `SIMULATOR_CONFIG` | SoftHier architecture configuration. |
| `SIMULATOR_APP` | Workload source directory. |
| `POWER_INTERVAL_PS` | GVSoC exchange interval in picoseconds. |
| `ICE_TARGET_TOP_DIE_CELLS` | Approximate top-die discretization target. |
| `DEFAULT_POWER_W` | Optional override for constant-power floorplan blocks. |
| `BUILD_SIMULATOR` | Set to `0` only when a compatible build already exists. |
| `BUILD_3DICE` | Set to `0` to require existing 3D-ICE binaries. |
| `DICE_RUN_MODE` | `local-server` or `client-server`. |
| `SIMULATOR_LOG_TAIL_LINES` | Live terminal window size; `0` disables it. |
| `ICE_GENERATE_GIF` | Generate an end-of-run temperature GIF when `1`. |

The compatibility aliases `CFG`, `APP`, `PLD`, `PWR_INTERVAL_PS`,
`BUILD_SOFTHIER`, and `SOFTHIER_LOG_TAIL_LINES` remain accepted.

### Choosing the Interval

By default:

```text
3D-ICE slot seconds = POWER_INTERVAL_PS × 1e-12
3D-ICE transient step seconds = slot seconds / 10
```

A smaller interval gives more thermal feedback points, but each interval also
executes the hook and one thermal solve. Choose the interval based on the
workload duration and required thermal resolution. The request trace records
the exact `start_ps` and `end_ps` of every interval.

Override the solver time base only deliberately:

```bash
make coupled-run \
  RUN_NAME=custom_timebase \
  POWER_INTERVAL_PS=100000000 \
  ICE_SLOT_SECONDS=0.0001 \
  ICE_STEP_SECONDS=0.00001
```

## 3D-ICE Run Modes

The default uses the server’s local follow mode:

```bash
make coupled-run RUN_NAME=default_app DICE_RUN_MODE=local-server
```

The server reads the run-local power file directly. No client is started.

Socket client/server mode remains available:

```bash
make coupled-run \
  RUN_NAME=socket_mode \
  DICE_RUN_MODE=client-server \
  PORT=54322
```

The feedback hook is identical in both modes; only transport from the power
trace file into the 3D-ICE server changes.

## Run Directory

A timestamped run is written under:

```text
runs/<run-name>/<timestamp>/
```

and linked from:

```text
runs/<run-name>/latest
```

Important files are:

```text
run.env
summary.txt
generated/
  system_config.json
  power_hook_config.json
  geo.json
  3dice/
    floorplan_nopower.flp
    ice.stk
traces/
  power_hook_trace.jsonl
  component_temperatures.csv
  3dice_power_traces.txt
results/3dice/
  output_top_die_flp_avg.txt
  output_top_die.txt
  xyaxis_TOP_DIE.txt
logs/
  simulator.log
  3dice_server.log
  3dice_client.log
state/
  power_hook_request.json
  power_hook_response.json
  simulator.done
```

`power_hook_trace.jsonl` is the authoritative GVSoC power history. Its entries
are `init`, zero or more complete `update` windows, then `final`.
`component_temperatures.csv` contains the initial and 3D-ICE-returned
temperatures. `3dice_power_traces.txt` is in exact generated floorplan order
and ends with one all-`-1` row.

## Reading Component Temperature

Inside the engine, models can use:

```cpp
double temperature_c = block->power.get_temperature();
```

The GVSoC socket proxy also exposes:

```python
temperature_c = proxy.get_component_temperature(
    "/chip/cluster_0/redmule"
)
```

`temperature_set_all()` stores the value on every visited component and updates
all local power sources. The shipped SoftHier redmule and memory tables
currently have only a 25 °C model point, so live temperatures propagate and
are observable but do not numerically change those models until
temperature-dependent table data is supplied.

## Geometry-Only Generation

Generate inputs without running the simulators:

```bash
make ice-inputs \
  RUN_NAME=geometry_only \
  SIMULATOR_CONFIG=$PWD/SoftHier/soft_hier_sdk/examples/SoftHier/config/arch_test.py \
  POWER_INTERVAL_PS=10000000
```

Or call the generic generator after exporting a contract:

```bash
SYSTEM_CONFIG_FILE=$PWD/runs/manual/system_config.json \
SIMULATOR_CONFIG=$PWD/SoftHier/soft_hier_sdk/examples/SoftHier/config/arch_test.py \
  Interface_scripts/providers/softhier/provider.sh export-system

python Interface_scripts/geometry_generator/generate_3dice_inputs.py \
  --system-config runs/manual/system_config.json \
  --geo runs/manual/geo.json \
  --floorplan runs/manual/floorplan_nopower.flp \
  --stk runs/manual/ice.stk \
  --power-interval-ps 10000000
```

## Plotting

Generate a GIF automatically:

```bash
make coupled-run RUN_NAME=default_app ICE_GENERATE_GIF=1
```

Generate or follow an HTML dashboard manually:

```bash
python Interface_scripts/plot_runtime_temperature_map/plot_runtime_tmap.py \
  --coords runs/<run-name>/latest/results/3dice/xyaxis_TOP_DIE.txt \
  --map runs/<run-name>/latest/results/3dice/output_top_die.txt \
  --html runs/<run-name>/latest/results/3dice/temperature_map.html \
  --follow \
  --poll 2
```

## Status and Cleanup

```bash
make latest-run RUN_NAME=default_app
make coupled-status RUN_NAME=default_app
make coupled-stop RUN_NAME=default_app
make list-runs
```

Cleanup targets are:

```bash
make clean-latest RUN_NAME=default_app
make clean-run RUN_DIR=$PWD/runs/default_app/<timestamp>
make clean-runs
```

## Validation

Run the interface regression suite:

```bash
make interface-tests
```

Check the selected provider and pinned revisions:

```bash
make simulator-check
git -C SoftHier status --short --branch
git -C SoftHier/engine status --short --branch
git -C SoftHier/soft_hier_sdk rev-parse HEAD
```

## Troubleshooting

### The hook times out waiting for Tflp

Inspect:

```bash
tail -n 50 runs/<run-name>/latest/logs/3dice_server.log
tail -n 50 runs/<run-name>/latest/logs/simulator.log
wc -l runs/<run-name>/latest/traces/3dice_power_traces.txt
wc -l runs/<run-name>/latest/results/3dice/output_top_die_flp_avg.txt
```

The server must be in follow mode, the stack must contain the top-die `Tflp`
output, and the floorplan header must match the generated order.

### The simulator exits on a hook protocol error

Inspect the last request, response, and simulator log:

```bash
cat runs/<run-name>/latest/state/power_hook_request.json
cat runs/<run-name>/latest/state/power_hook_response.json
tail -n 100 runs/<run-name>/latest/logs/simulator.log
```

Paths must be exact and declared at `init`; powers must be finite and
non-negative; responses must use protocol version 1 and Celsius.

### A very small interval runs slowly

Version 1 directly executes the Python adapter once per exchange. Increase
`POWER_INTERVAL_PS` for smoke tests or short workloads. This changes thermal
sampling resolution, not GVSoC’s simulated functional behavior.
