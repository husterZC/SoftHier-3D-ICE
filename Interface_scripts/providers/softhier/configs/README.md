# Memory-on-logic model inputs

The provider uses these files for architectures with `dram3d_enable=1`:

- `dram3d_thermal.json`: physical thicknesses and material properties.
- `dram3d_power.json`: HBM2 core electrical parameters, consumed separately by
  the native DRAM power integration.

These defaults are engineering starting assumptions, not measured HBM2 package
or device characterization. Both resolved configurations and their SHA-256
hashes are recorded in the system contract. Override them using
`SOFTHIER_THERMAL_CONFIG` and `SOFTHIER_DRAM_POWER_CONFIG`, or the exporter's
`--thermal-config` and `--dram-power-config` arguments.

## Physical package

One DRAM die covers the logic die. Each DRAM channel occupies exactly the
rectangle of its corresponding cluster, with channel index
`cid = y * num_cluster_x + x`.

The HBM2 memory specification retains the SDK's logical capacity and timing
topology. Its stack/bank fields do not create additional thermal tiers; the
physical model has the single DRAM die agreed for this experiment. The active
faces point toward the bond:

```text
                         top cooling boundary
                         copper heatsink
                         four TIM layers
                         DRAM bulk silicon
                         DRAM active source
                         face-to-face bond
                         logic active source
                         logic bulk silicon
                         package bumps
                         four substrate layers
```

The default logic and DRAM dies are each 50 µm thick. Each includes a 1 µm
source layer and 49 µm of bulk; the source layer does not add to the total die
thickness. The 5 µm bond uses the existing package bump's effective anisotropic
material as an initial assumption. It is not a claim about a particular hybrid
bonding process. The existing substrate, bumps, TIM, copper heatsink, and
15,000 W/(m²·K) cooling boundary are preserved. The resulting default package
has 15 physical layers and a total thickness of 1,235 µm.

Input dimensions are in micrometres. Conductivity is in W/(m·K); heat capacity
is in J/(m³·K). Conductivity may be one scalar or a three-element array in
x/y/z order. The stack generator converts SI properties into 3D-ICE's
micrometre units. All values must be finite and positive; each source thickness
must be smaller than its corresponding total die thickness. A nonempty
`provenance` field is required for overrides.

## Generated interface

Memory-on-logic uses system contract version 2. Floorplan regions and feedback
components have a `die` field (`LOGIC_DIE` or `DRAM_DIE`). `thermal_stack`
records the physical materials, bottom-to-top layers, die orientation, and
cooling boundary. Version 1 remains supported for single-die architectures.

The geometry generator writes one floorplan and temperature output pair per
die plus `thermal_manifest.json`. Manifest filenames are relative to the
manifest; floorplan element names use the flattened 3D-ICE names. Its
`power_order` lists every floorplan region, including constant-power residual
regions, in bottom-to-top source order: logic first, then DRAM. This order is
the reverse of the powered die declarations in the `.stk` file.

The multi-die generator uses one factorization thread, as the thermal replay
workflow does. Parallel SuperLU_MT factorization produced unphysical
sub-ambient temperatures in a full 4×4 positive-power validation case; the
single-thread solver preserves the ambient equilibrium and heating lower
bound. The legacy single-die generator retains its original thread setting.

Logic components retain their technology's initialization voltage. DRAM
components do not inherit that logic voltage; their electrical rails are part
of `dram3d_power.json`. DRAM controller and PHY power are excluded. The
`temperature_aware` profile describes logic leakage; the initial DRAM model
uses fixed electrical characterization and does not infer temperature scaling.
