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
make cosim RUN_NAME=default_app
```

See [cosim.md](cosim.md) for the detailed tutorial, run directory layout,
alternate config/app examples, and cleanup targets.

## 🚀 Setup

Initialize the submodules and set up the SoftHier environment:

```bash
source init.sh
```

## 🧩 Generate 3D-ICE Inputs

1. Generate the geometry file:

```bash
python Interface_scripts/geometry_generator/geogen.py
```

This uses `SoftHier/soft_hier/flex_cluster/flex_cluster_arch.py` and writes
`SoftHier/geo.json`.

2. Generate the 3D-ICE floorplan:

```bash
python Interface_scripts/geometry_generator/roi2ice_floorplan_no_power.py \
  SoftHier/soft_hier/flex_cluster/flex_cluster_arch.py \
  SoftHier/geo.json \
  3D-ICE/test_interface_softhier
```

This writes `3D-ICE/test_interface_softhier/floorplan_nopower.flp`.

3. Generate the 3D-ICE stack file:

```bash
python 3D-ICE/test_interface_softhier/scripts/roi2ice_stk.py \
  3D-ICE/test_interface_softhier/floorplan_nopower.flp \
  3D-ICE/test_interface_softhier/example_transient_test_server.stk
```

This writes `3D-ICE/test_interface_softhier/example_transient_test_server.stk`.

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

### 1. Start the 3D-ICE Server

On the server machine, run:

```bash
cd 3D-ICE/test_interface_softhier
../bin/3D-ICE-Server ./example_transient_test_server.stk 54322
```

Keep this terminal open and wait until the server is waiting for a client.

### 2. Start the 3D-ICE Client

#### 2.1 Open an SSH tunnel:

```bash
ssh -N -L 54322:127.0.0.1:54322 <user>@<server>
```

Keep this tunnel terminal open while the simulation is running.

#### 2.2 Open a second terminal, and launch 3D-ICE client:

From `3D-ICE/test_interface_softhier`, run:

```bash
../bin/3D-ICE-Client 127.0.0.1 54322 power_traces.txt --follow --until-minus-one
```

### Optional: Plot Runtime Temperature

Install the plotting requirements:

```bash
python -m pip install -r 3D-ICE/test_interface_softhier/requirements_tmap_plot.txt
```

The stack file must include a `Tmap` output for the live map files to be produced:

```text
Tmap( TOP_DIE,    "output_top_die_map.txt",                 slot );
```

From `3D-ICE/test_interface_softhier`, run:

```bash
python plot_runtime_tmap.py --coords output_top_die_map.coords.txt --map output_top_die_map.txt
```
