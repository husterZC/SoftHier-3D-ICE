# 🧊 SoftHier-3D-ICE

SoftHier-3D-ICE couples a simulator power provider to a local 3D-ICE thermal
workflow. SoftHier is the default provider, while geometry generation and the
3D-ICE hook consume a simulator-neutral JSON contract.

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
See [Interface_scripts/README.md](Interface_scripts/README.md) for the provider
API and instructions for changing or replacing the SoftHier submodule.

## 🚀 Setup

Initialize the submodules and set up the SoftHier environment:

```bash
source init.sh
```


## 🧩 Generate 3D-ICE Inputs

For run-local generated files, use the root target:

```bash
make ice-inputs RUN_NAME=default_app POWER_INTERVAL_PS=100000000
```

This writes timestamped files under:

```text
runs/default_app/<timestamp>/generated/
  system_config.json
  geo.json
  3dice/
    floorplan_nopower.flp
    ice.stk
```

The selected provider first exports `system_config.json`. The generic geometry,
floorplan, and stack generators consume that file; they do not import SoftHier
or call SoftHier's `ice_prepare` target.

Equivalent direct command:

```bash
SYSTEM_CONFIG_FILE=$PWD/runs/manual/generated/system_config.json \
  Interface_scripts/providers/softhier/provider.sh export-system

python Interface_scripts/geometry_generator/generate_3dice_inputs.py \
  --system-config runs/manual/generated/system_config.json \
  --geo runs/manual/generated/geo.json \
  --floorplan runs/manual/generated/3dice/floorplan_nopower.flp \
  --stk runs/manual/generated/3dice/ice.stk \
  --power-interval-ps 100000000
```

The stack slot duration is generated from `POWER_INTERVAL_PS * 1e-12` by default,
so one provider power row corresponds to one 3D-ICE thermal slot. The transient
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

The root runner generates run-local 3D-ICE inputs, starts the 3D-ICE server in
`DICE_RUN_MODE=local-server`, then runs the provider. GVSoC directly invokes
the versioned hook after each complete interval: the hook appends one 3D-ICE
power slot, waits for `Tflp`, converts Kelvin to component temperatures in
Celsius, and returns them to GVSoC. The final partial interval is reported but
not applied. Results are written under `runs/<run-name>/<timestamp>/`.

In local-server mode no 3D-ICE client is started. The server reads
`traces/3dice_power_traces.txt` with `--follow --until-minus-one`.


During the simulator phase, the terminal shows a green framed live window with
the latest 5 provider log lines. Change the window size with
`SIMULATOR_LOG_TAIL_LINES=10`, or disable it with
`SIMULATOR_LOG_TAIL_LINES=0`.

For isolated 3D-ICE debugging, use the generated stack file from a run directory
with the binaries in `3D-ICE/bin/` and a compatible 3D-ICE power trace.

Example:

```bash
REPO_ROOT=$PWD
RUN_DIR=$PWD/runs/manual make ice-inputs POWER_INTERVAL_PS=100000000
```

Then run `3D-ICE/bin/3D-ICE-Server` with the generated stack and a compatible
power trace:

```bash
cd runs/manual/generated/3dice
"$REPO_ROOT/3D-ICE/bin/3D-ICE-Server" ice.stk   --power-trace ../../traces/3dice_power_traces.txt   --follow   --until-minus-one
```

### Optional: Plot Runtime Temperature and GIF

For SSH/headless runs, write a self-contained HTML dashboard from the full
Tmap output. The page embeds all complete slots currently present in
`output_top_die.txt`, opens at the latest slot by default, and provides a slot
slider plus `Previous`, `Next`, and `Latest` buttons:

```bash
python Interface_scripts/plot_runtime_temperature_map/plot_runtime_tmap.py   --coords runs/<run-name>/latest/results/3dice/xyaxis_TOP_DIE.txt   --map runs/<run-name>/latest/results/3dice/output_top_die.txt   --html runs/<run-name>/latest/results/3dice/temperature_map.html   --follow   --poll 2
```

Open `runs/<run-name>/latest/results/3dice/temperature_map.html` with a browser.
In `--follow` mode, the script rewrites the HTML when complete Tmap slots are
appended, and the page refreshes at the `--poll` interval. Manual slot selection
is preserved across refreshes; click `Latest` to resume following the newest
slot. 

To generate a dashboard-style animated GIF after the run finishes, enable the
end-of-run export:

```bash
make coupled-run RUN_NAME=default_app ICE_GENERATE_GIF=1
```

The GIF is written to `runs/<run-name>/latest/results/3dice/temperature_map.gif`
by default. It uses the same `xyaxis_TOP_DIE.txt` and `output_top_die.txt`
source files as the HTML dashboard. 

You can also generate the GIF manually after a run:

```bash
python Interface_scripts/plot_runtime_temperature_map/plot_runtime_tmap.py   --coords runs/<run-name>/latest/results/3dice/xyaxis_TOP_DIE.txt   --map runs/<run-name>/latest/results/3dice/output_top_die.txt   --gif runs/<run-name>/latest/results/3dice/temperature_map.gif   --once
```
