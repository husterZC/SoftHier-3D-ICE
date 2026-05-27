# 🧊 SoftHier-3D-ICE

## 🚀 Get Started

First, run the following command to clone the submodules and set up the SoftHier environment:

```bash
source init.sh
```

## 🧩 SoftHier

Navigate to the `SoftHier` directory:

```bash
cd SoftHier
```

Generate a geometry description file:

```bash
make geo
```

This creates `geo.json` using the default architecture configuration:

```text
soft_hier/flex_cluster/flex_cluster_arch.py
```

To use a custom architecture configuration, run:

```bash
cfg=<path-to-architecture-config> make geo
```


