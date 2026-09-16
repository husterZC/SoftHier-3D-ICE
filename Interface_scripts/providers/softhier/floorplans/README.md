# SoftHier floorplan rules

Placement rules are separate from architecture interpretation, component area
estimation, power coefficients, and thermal-feedback mappings. The exporter
supplies a component inventory; a rule returns rectangular regions and the
cluster dimensions. The exporter replicates that cluster on the architecture's
`num_cluster_x × num_cluster_y` grid.

## Available rules

| Rule | Cluster layout |
|---|---|
| `redmule_strip` (default) | Original layout: square RedMulE at lower left, full-height TCDM strip on the right, binary-partitioned support region above RedMulE. |
| `square_bands` | Square cluster with three full-width horizontal bands: RedMulE/TCDM at the bottom, PE/Spatz pairs in the middle, support components at the top. |

The default retains the earlier experiment geometry. These are both area-based
approximations, not placed-and-routed layouts.

### Square bands

Schematic only; widths/heights are determined by area, not equal-sized boxes:

```text
                 top (+y)
┌───────────────────────────────────────────────────┐
│ instruction │ stack │ iDMA(s) │ NoC/NI │ rest      │  c
├─────────────┬─────────────┬─────────────┬───────────┤
│    pe0      │    pe1      │    ...      │           │
├─────────────┼─────────────┼─────────────┤ unpaired  │  b
│   spatz0    │   spatz1    │    ...      │    peN    │
├─────────────┴─────────────┴─────────────┴───────────┤
│                 RedMulE                 │  TCDM   │  a
└────────────────────────────────────────┴──────────┘
                 bottom (y = 0)
```

- The cluster side is `sqrt(sum(component areas))`.
- Each band's height is `sum(band areas) / cluster side`.
- Bottom: RedMulE on the left and TCDM on the right, at the same height.
- Middle: one column per core, ordered by numeric core index. Column widths
  follow the combined PE/Spatz areas. Within each column, Spatz is below its
  PE. A core without Spatz fills its column; no fictitious vector region is added.
- Top: left-to-right instruction memory, stack memory, iDMA instance(s),
  router/interface instances, then remaining components. All share the band's
  height and receive widths proportional to their areas. The current remaining
  components include transpose and `others`.

Both rules use exactly the same areas, including the original `others` reserve:
5% of modeled area excluding RedMulE and TCDM. This is added once, not once per
band. Its power still follows `DEFAULT_POWER_W` (zero by default).

## Select a rule

From the repository root, keep the normal app/architecture/preload settings and
add the floorplan selector:

```sh
make coupled-run SOFTHIER_FLOORPLAN=square_bands
# The original layout:
make coupled-run SOFTHIER_FLOORPLAN=redmule_strip
```

Generate inputs without running the simulator (use the configured Python
environment):

```sh
make ice-inputs RUN_NAME=square_layout \
  SIMULATOR_CONFIG="$PWD/SoftHier/soft_hier_sdk/implementation/config/arch/arch.py" \
  SOFTHIER_FLOORPLAN=square_bands
```

The exporter also accepts `--floorplan square_bands`. Direct provider calls use
`SOFTHIER_FLOORPLAN`; the root Makefile exports it to all subprocesses. The
selection is recorded in `generated/system_config.json` as
`metadata.floorplan_rule`, in provider `run.env` entries, and in study manifests.

For implementation-kernel pairs:

```sh
conda run --no-capture-output -n py312 python experiments/component_power/run_study.py \
  --prefix square_norm --kernels norm --floorplan square_bands
```

Add `--build-hardware` when a compatible simulator has not yet been built. A
placement-only change does not require rebuilding native simulator models.
The shared analysis refuses to merge studies with different layout rules into
one ordinary leakage-profile comparison. Use separate result directories for
layout comparisons. The original results have not been regenerated or relabeled.

## Add another rule

1. Add a module beside `square_bands.py` with `DESCRIPTION` and
   `build(inventory) -> (regions, [width, height])`.
2. Register it in `RULES` in `__init__.py`; the exporter/study CLI choices then
   pick it up. Update the shell help and this documentation too.
3. Return one leaf rectangle per inventory name, including the already-added
   `others` region. Use µm for lengths and µm² for areas. Do not change the
   inventory, power paths, or region names.
4. Add tests for the rule's placement constraints. The shared validator checks
   domain coverage, area conservation, positive dimensions, bounds, and overlap.

The native rule interface is deliberately a geometry-only interface. Physical
placement does not reroute simulator traffic or change application execution
timing. Its thermal effect requires a new co-simulation; previously generated
temperature maps cannot simply be reused with new positions.
