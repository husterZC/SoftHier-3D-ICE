# 🧊 SoftHier-3D-ICE

SoftHier-3D-ICE generates 3D-ICE inputs from the default SoftHier architecture and
runs them through the 3D-ICE server/client workflow.

Unless noted otherwise, run commands from the repository root.

## Recommended Co-Simulation Flow

For the current root-level SoftHier + 3D-ICE coupled flow, start with:

```bash
make bootstrap
make coupled-run RUN_NAME=default_app
```

For one-command first setup and run:

```bash
make co-simulation RUN_NAME=default_app
```

See [co-simulation.md](co-simulation.md) for the detailed tutorial, run directory layout,
alternate config/app examples, and cleanup targets.

## 🚀 Setup

Initialize the submodules and set up the SoftHier environment:

```bash
source init.sh
```

## 🧩 Generate 3D-ICE Inputs

For run-local generated files, use the root target:

```bash
make ice-inputs RUN_NAME=default_app PWR_INTERVAL_PS=100000000
```

This writes timestamped files under:

```text
runs/default_app/<timestamp>/generated/
  geo.json
  3dice/
    floorplan_nopower.flp
    ice.stk
```

The target uses `Interface_scripts/geometry_generator/generate_3dice_inputs.py`,
which runs the root geometry, floorplan, and stack generators. It does not call
SoftHier's `ice_prepare` target.

Equivalent direct command:

```bash
python Interface_scripts/geometry_generator/generate_3dice_inputs.py \
  --arch SoftHier/soft_hier/flex_cluster/flex_cluster_arch.py \
  --geo runs/manual/generated/geo.json \
  --floorplan runs/manual/generated/3dice/floorplan_nopower.flp \
  --stk runs/manual/generated/3dice/ice.stk \
  --pwr-interval-ps 100000000
```

The stack slot duration is generated from `PWR_INTERVAL_PS * 1e-12` by default,
so one SoftHier power row corresponds to one 3D-ICE thermal slot. The transient
solver step defaults to one tenth of that slot.

## 🧩 Build 3D-ICE

Install the Python requirements:

```bash
python -m pip install -r 3D-ICE/requirements.txt
```

Build 3D-ICE:

```bash
cd 3D-ICE
bash install-superlumt.sh
make
cd ..
```

## 🧩 Run the Simulation

For normal co-simulation, use the root target:

```bash
make co-simulation RUN_NAME=default_app
```

Or, after `make bootstrap` has completed:

```bash
make coupled-run RUN_NAME=default_app
```

The root runner generates run-local 3D-ICE inputs, starts the 3D-ICE server,
starts the trace adapter, starts the 3D-ICE client with
`--follow --until-minus-one`, runs SoftHier, and writes results under
`runs/<run-name>/<timestamp>/`.

During the SoftHier phase, the terminal shows a live tail of the latest 5
SoftHier log lines. Change the window size with `SOFTHIER_LOG_TAIL_LINES=10`,
or disable it with `SOFTHIER_LOG_TAIL_LINES=0`.

For isolated 3D-ICE debugging, use the generated stack file from a run directory
with the binaries in `3D-ICE/bin/` and a compatible 3D-ICE power trace.

Example:

```bash
RUN_DIR=$PWD/runs/manual make ice-inputs PWR_INTERVAL_PS=100000000
```

Then run `3D-ICE/bin/3D-ICE-Server` with
`runs/manual/generated/3dice/ice.stk`.

### Optional: Plot Runtime Temperature

Install the plotting requirements:

```bash
python -m pip install -r Interface_scripts/plot_runtime_temperature_map/requirements_tmap_plot.txt
```

The stack file must include a `Tmap` output for the live map files to be produced:

```text
Tmap( TOP_DIE,    "output_top_die_map.txt",                 slot );
```

From the repository root, run:

```bash
python Interface_scripts/plot_runtime_temperature_map/plot_runtime_tmap.py \
  --coords runs/<run-name>/latest/results/3dice/output_top_die_map.coords.txt \
  --map runs/<run-name>/latest/results/3dice/output_top_die_map.txt
```
