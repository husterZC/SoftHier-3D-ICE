# Stacked logic/DRAM model inputs

The provider uses these files for architectures with `dram3d_enable=1`:

- `dram3d_thermal.json`: die stack order, physical thicknesses, and material properties.
- `dram3d_power.json`: HBM2 core electrical parameters, consumed separately by
  the native DRAM power integration.

These defaults are engineering starting assumptions, not measured HBM2 package
or device characterization. Both resolved configurations and their SHA-256
hashes are recorded in the system contract. Override them using
`SOFTHIER_THERMAL_CONFIG` and `SOFTHIER_DRAM_POWER_CONFIG`, or the exporter's
`--thermal-config` and `--dram-power-config` arguments.

## Select the stack order

Set the top-level `stack_order` field in the thermal JSON to one of two values:

| `stack_order` | Top die, nearest the heatsink | Bottom die, nearest the package bumps |
| --- | --- | --- |
| `logic_on_dram` | Logic | DRAM |
| `dram_on_logic` (default) | DRAM | Logic |

For logic on top, change the field to `"stack_order": "logic_on_dram"`.
For DRAM on top, use `"stack_order": "dram_on_logic"`. Older configurations
that omit the field retain DRAM on top. Other values are rejected; the thermal
configuration schema remains version 1. The normalized selection is archived
with the resolved thermal inputs and their hash.

You can edit the default JSON or save a complete copy with the selected value
inside the repository. Pass the copy to `experiments/llm_dram3d/run_study.py`
using `--thermal-config path/to/config.json`, or set
`SOFTHIER_THERMAL_CONFIG=path/to/config.json` for the provider/Make workflow.
No separate stack-order command-line option is needed.

## Physical package

One logic die and one DRAM die share a footprint. Each DRAM channel occupies
exactly the rectangle of its corresponding cluster, with channel index
`cid = y * num_cluster_x + x`.

The HBM2 memory specification retains the SDK's logical capacity and timing
topology. Its stack/bank fields do not create additional thermal tiers; the
physical model has the single DRAM die agreed for this experiment. Both orders
use face-to-face bonding. The active faces point toward the bond, with each
die's BEOL between its active source and the bond. The default `dram_on_logic`
arrangement is shown top to bottom:

```text
                         top cooling boundary
                         copper heatsink
                         four TIM layers
                         DRAM bulk silicon
                         DRAM active source
                         DRAM BEOL (5 µm, placeholder)
                         hybrid-bond region (2 µm)
                         logic BEOL (5 µm)
                         logic active source
                         logic bulk silicon
                         package bumps
                         four substrate layers
```

For `logic_on_dram`, the arrangement from **bottom to top** is:

```text
substrate → package bumps → DRAM bulk → DRAM source → DRAM BEOL
→ hybrid bond → logic BEOL → logic source → logic bulk
→ TIM → copper heatsink → top cooling boundary
```

Cooling stays above the top die. The bottom die has `active_face: top`; the
top die has `active_face: bottom`. Each die retains its own materials,
thicknesses, floorplan, and source depth when the order changes.

The default logic and DRAM dies are each **55 µm thick**: 49 µm bulk silicon,
1 µm active source, and 5 µm BEOL. The silicon thickness is preserved; BEOL is
added to the previous 50 µm die. The 2 µm hybrid-bond region replaces the
previous 5 µm effective F2F bond. The substrate, package bumps, TIM, copper
heatsink, and 15,000 W/(m²·K) cooling boundary retain their existing settings.
With these defaults, either order has **17 physical layers**, totaling **1,242 µm**.
Both BEOL regions and the bond are passive; power remains assigned to the two
active source layers with the existing logic/DRAM floorplans.

The BEOL conductivity is our homogenization of the two layers in Table 2 of
[Naeim et al.'s thermal study](https://www.cadence.com/ja_JP/home/resources/white-papers/thermal-analysis-3d-stacking-beol-technologies.html)
([ISVLSI 2024 paper](https://doi.org/10.1109/ISVLSI61997.2024.00018)):
1.5 µm at 1.5 W/(m·K), followed by 3.5 µm at 2.5 W/(m·K). Thickness-weighted
parallel averaging gives `kx = ky = 2.2 W/(m·K)`; series resistance gives
`kz = 5 / (1.5/1.5 + 3.5/2.5) = 2.0833333333333335 W/(m·K)`.

The **DRAM BEOL thickness and conductivity are placeholders** copied from the
logic model pending DRAM-specific data. Both BEOL heat capacities provisionally
reuse silicon's `1.61e6 J/(m³·K)`; these are not values reported by that paper.

The nominal BEOL and hybrid-bond parameters are:

| Region | Thickness (µm) | Conductivity `[kx, ky, kz]` (W/(m·K)) | Volumetric heat capacity (J/(m³·K)) |
| --- | ---: | --- | ---: |
| Logic BEOL | 5 | `[2.2, 2.2, 2.0833333333333335]` | `1.61e6` |
| DRAM BEOL | 5 | `[2.2, 2.2, 2.0833333333333335]` | `1.61e6` |
| Hybrid bond | 2 | `[1.851145038167939, 1.851145038167939, 17.0]` | `2.0452e6` |

The hybrid-bond baseline assumes **20% copper coverage** (`f = 0.20`), with
uniformly distributed cylindrical pads spanning the full bond thickness, so
the area and volume fractions coincide. Effective pad conductivity
`km = 80 W/(m·K)` and dielectric conductivity `kd = 1.25 W/(m·K)` are the
midpoints of the respective 60–100 and 1–1.5 ranges in Table 1 of the
[Naeim et al. study](https://www.cadence.com/ja_JP/home/resources/white-papers/thermal-analysis-3d-stacking-beol-technologies.html).
The phase values are literature inputs; the coverage, pad geometry, and
homogenization are modeling assumptions for this experiment.

Through-plane conduction uses the parallel average
`kz = f*km + (1-f)*kd = 17 W/(m·K)`. In-plane conduction uses the
[Maxwell approximation for circular cylindrical inclusions](https://doi.org/10.1007/s00707-020-02788-3):
`beta = (km-kd)/(km+kd)` and
`kx = ky = kd*(1+f*beta)/(1-f*beta) = 1.851145038167939 W/(m·K)`.
The bond uses effective room-temperature constants and ideal thermal contact,
with no additional interface resistance. Its through-plane areal resistance
is `2e-6/17 = 1.176470588235294e-7 m²·K/W`.

For heat capacity, copper retains the existing model's `3.45e6 J/(m³·K)`.
The dielectric uses a bulk fused-silica proxy from the
[Corning HPFS brochure](https://www.corning.com/content/dam/corning/media/worldwide/csm/documents/HPFS_Product_Brochure_All_Grades_2015_07_21.pdf):
density `2200 kg/m³` and specific heat `770 J/(kg·K)` at 25 °C give
`Cv_dielectric = 1.694e6 J/(m³·K)`. Volume averaging gives
`Cv_bond = f*3.45e6 + (1-f)*1.694e6 = 2.0452e6 J/(m³·K)`.
This is an estimate for a Cu/dielectric region, not characterization of the
actual bonding process. The package bumps retain their separate original
material properties. All assumptions are recorded in the JSON provenance.

### Sensitivity settings

The following settings describe optional future sensitivity studies; they do
not run automatically. These are exploration ranges, not confidence intervals.
Vary one input at a time from the nominal configuration unless deliberately
studying interactions.

| Input | Nominal | Exploration values |
| --- | --- | --- |
| Hybrid-bond Cu coverage `f` | `0.20` | `0.05`, `0.20` |
| Effective Cu pad conductivity `km` (W/(m·K)) | `80` | `60`, `80`, `100` |
| Dielectric conductivity `kd` (W/(m·K)) | `1.25` | `1.0`, `1.25`, `1.5` |
| Logic BEOL heat capacity (J/(m³·K)) | `1.61e6` | `1.0e6`, `1.61e6`, `2.3e6` |
| DRAM BEOL heat capacity (J/(m³·K)) | `1.61e6` | `1.0e6`, `1.61e6`, `2.3e6` |
| DRAM BEOL conductivity tensor scale | `1×` | `0.5×`, `1×`, `2×` |

The BEOL heat-capacity values and DRAM conductivity scales are engineering
exploration choices. Keep each BEOL heat capacity independently adjustable;
apply the DRAM conductivity scale to all three tensor entries. The configuration
stores effective properties, not `f`, `km`, or `kd`: when varying these hybrid
inputs, recalculate the bond conductivity with the formulas above and, if
coverage changes, its heat capacity too. Save the resolved values and updated
provenance in an override JSON and select it with `SOFTHIER_THERMAL_CONFIG`.

### Configuration fields and units

Input dimensions are in micrometres. Conductivity is in W/(m·K); heat capacity
is in J/(m³·K). Conductivity may be one scalar or a three-element array in
x/y/z order. The stack generator converts SI properties into 3D-ICE's
micrometre units. All dimensions and material properties must be finite and
positive; `source_thickness_um + beol.thickness_um` must be smaller than the
die's `thickness_um`, leaving positive bulk thickness. Each die's optional
`beol` object contains its thickness, conductivity, and volumetric heat capacity.
Omitting it preserves the legacy source/bulk-only die. `thickness_um` always
includes all sublayers, including BEOL when present. The optional
`face_to_face_bond.technology` is `hybrid` for the new defaults, or `effective`
(also the legacy default when omitted). A nonempty `provenance` field is
required for overrides.

## Generated interface

Both stack orders use system contract version 2. Floorplan regions and feedback
components have a `die` field (`LOGIC_DIE` or `DRAM_DIE`). `thermal_stack`
records the physical materials, bottom-to-top layers, die orientation, and
cooling boundary. A die's optional `source_depth_um` is the thickness of passive
layers between its active face and the nearest source boundary, excluding the
source itself. It is 5 µm for each default BEOL and defaults to zero for older
contracts. The validator checks this depth against the actual layer order.
Version 1 remains supported for single-die architectures.

The new defaults apply when exporting a new system contract. Existing runs
and portable replay recordings retain their archived stack and material
properties; replay does not reload this provider configuration. Importing an
old run with `record` also retains that old run's stack.

The geometry generator writes one floorplan and temperature output pair per
die plus `thermal_manifest.json`. Manifest filenames are relative to the
manifest; floorplan element names use the flattened 3D-ICE names. Its
`power_order` lists every floorplan region, including constant-power residual
regions, in bottom-to-top source order: logic then DRAM for `dram_on_logic`,
and DRAM then logic for `logic_on_dram`. This order is the reverse of the
powered die declarations in the `.stk` file. Raw simulator power columns and
component identities keep their original order; the adapter and replay mapping
place them into the physical source order.

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
