# Simulator and Thermal Interface

`Interface_scripts` keeps simulator integration, geometry generation, and
thermal simulation separate. The normal closed-loop flow uses three versioned
boundaries:

1. a provider executable for simulator lifecycle operations;
2. a JSON system contract owned by the architecture side; and
3. the GVSoC `gvsoc-power-hook` JSON request/response protocol.

The GVSoC engine knows component paths, powers, and temperatures. It does not
know about floorplans, 3D-ICE, Kelvin conversion, or geometry generation.

## Closed-Loop Data Flow

```text
SoftHier architecture configuration
             |
             | provider: export-system
             v
generated/system_config.json
  - physical geometry
  - exact GVSoC component paths
  - power-column and floorplan mappings
             |
             | generic generators
             v
3D-ICE floorplan + stack, including Tflp output
             |
             | start 3D-ICE in follow mode
             v
GVSoC interval event
  -> average subtree power in W
  -> direct executable invocation (no shell)
  -> 3dice_power_hook.py appends one floorplan power slot
  -> 3D-ICE solves one thermal slot
  -> hook reads Tflp temperatures in K
  -> area-weighted component temperatures in C
  -> GVSoC recursively applies returned temperatures
```

`ice_trace_adapter.py` remains available for offline and legacy raw-trace
conversion. It is not a process in the normal live feedback loop.

## Provider Lifecycle

`SIMULATOR_PROVIDER` is an executable receiving one action:

| Action | Required behavior |
|---|---|
| `name` | Print a human-readable provider name and nothing else. |
| `default-config` | Print the default simulator configuration path. |
| `bootstrap` | Initialize and pin simulator dependencies. |
| `check` | Verify that the required engine interface is present. |
| `export-system` | Write `SYSTEM_CONFIG_FILE`. |
| `build` | Build the selected architecture and workload. |
| `run` | Run in the foreground with the configured power hook. |
| `manifest` | Print shell-escaped `KEY=value` provenance entries. |

The runner supplies these common values:

```text
ROOT_DIR
RUN_DIR
SIMULATOR_CONFIG
SIMULATOR_APP
SIMULATOR_PLATFORM
POWER_INTERVAL_PS
SOFTHIER_POWER_PROFILE
SYSTEM_CONFIG_FILE
GEO_FILE
DEFAULT_POWER_W
POWER_HOOK_EXECUTABLE
POWER_HOOK_CONFIG_FILE
POWER_HOOK_REQUEST_FILE
POWER_HOOK_RESPONSE_FILE
POWER_HOOK_TRACE_FILE
PYTHON
MAKE
```

The provider owns all simulator-specific setup and command-line details.
`run` must remain in the foreground so the generic orchestrator can track its
status.

The SoftHier provider accepts `SOFTHIER_POWER_PROFILE=constant` or
`temperature_aware`, records the selection in its manifest, and passes it to
the target generator. Other providers may ignore this SoftHier-specific value.

## JSON System Contract

The authoritative validator is
[`system_contract.py`](system_contract.py). The discriminator is
`3dice-cosim-system`: version `1` describes the original single-die interface,
and version `2` adds explicit physical dies and stack layers. Existing version
1 documents remain supported.

A compact valid example is:

```json
{
  "contract": {
    "name": "3dice-cosim-system",
    "version": 1
  },
  "producer": {
    "name": "my-simulator"
  },
  "geometry": {
    "chip": {
      "type": "die",
      "shape": [10.0, 10.0],
      "offset": [0.0, 0.0],
      "subs": {
        "compute": {
          "type": "comp",
          "shape": [8.0, 10.0],
          "offset": [0.0, 0.0],
          "subs": {}
        },
        "memory": {
          "type": "comp",
          "shape": [2.0, 10.0],
          "offset": [8.0, 0.0],
          "subs": {}
        }
      }
    }
  },
  "floorplan": {
    "elements": [
      {
        "name": "chip/compute",
        "power": {"column": "compute_w"}
      },
      {
        "name": "chip/memory",
        "power": {"constant_w": 0.0}
      }
    ]
  },
  "power_trace": {
    "format": "whitespace-float-rows",
    "unit": "W",
    "columns": ["compute_w"]
  },
  "thermal_feedback": {
    "temperature_unit": "C",
    "initial_temperature_c": 26.85,
    "components": [
      {
        "path": "/chip/compute",
        "power_column": "compute_w",
        "floorplan_elements": ["chip/compute"],
        "aggregation": "area-weighted-average"
      }
    ]
  }
}
```

Geometry lengths are in micrometres. Component paths are exact absolute GVSoC
paths. Every sampled path represents its complete subtree.

Version 1 validation enforces:

- canonical, non-overlapping component paths;
- finite temperatures in Celsius above absolute zero;
- unique power columns, each used by exactly one floorplan element;
- one thermal-feedback component for every power column;
- known leaf floorplan elements and geometry paths;
- finite, non-negative constant power; and
- an explicit area-weighted mapping from floorplan temperatures back to each
  component.

The exact-path and one-column rules prevent hierarchical or floorplan double
counting. A future contract version can add explicit power-distribution
fractions if one component must be split across multiple powered blocks.

### Stacked logic/DRAM contract and package

For a stacked-only SoftHier architecture with `dram3d_enable=1`, the exporter
produces version 2. The SDK's 4×4 architecture has 304 logic power domains plus
16 DRAM channel domains, and 336 floorplan regions including the logic residual
regions. One DRAM die is divided into channel regions; channel
`cid = y * num_cluster_x + x` has exactly the corresponding cluster's footprint.

Version 2 retains the existing geometry, power columns, and feedback fields.
Every floorplan element and feedback component also identifies its `die`.
`thermal_stack` supplies materials in SI units, bottom-to-top physical layers,
die thicknesses and active-face orientations, and the top cooling boundary.
The validator checks complete per-die coverage, disjoint rectangles, matching
footprints, contiguous die layers, and one correctly positioned source layer
per die. Logic and DRAM components receive their own source-layer temperatures.
The optional per-die `source_depth_um` measures passive material from the active
face to the nearest source boundary (zero for legacy contracts), allowing BEOL
between the source and the bond without moving the heat source into BEOL.

The thermal configuration's `stack_order` selects `dram_on_logic` (the
default, also used when omitted) or `logic_on_dram`. The bottom die faces
upward and the top die faces downward, keeping their BEOL regions next to the
bond. Cooling stays above the top die. Each die retains its geometry and
material properties; solver power order follows the bottom-to-top sources.

The default bottom-to-top package is substrate → package bumps → logic bulk →
logic source → logic BEOL → hybrid bond → DRAM BEOL → DRAM source → DRAM bulk →
TIM → copper heatsink → top cooling boundary. Each die is 55 µm thick: 49 µm
bulk, 1 µm source, and 5 µm BEOL; the bond is 2 µm. The 17-layer package totals
1,242 µm. DRAM BEOL copies the logic placeholder pending characterization.
BEOL conductivity is derived from a published two-layer model; BEOL heat
capacity and bond material properties remain provisional. These engineering
assumptions have provenance recorded in the contract. Existing recordings
retain their archived stacks. See the complete
[input configuration documentation](providers/softhier/configs/README.md).

Override the checked-in inputs with `SOFTHIER_THERMAL_CONFIG` and
`SOFTHIER_DRAM_POWER_CONFIG`, or the exporter's `--thermal-config` and
`--dram-power-config` arguments. Resolved inputs and their hashes are archived
in contract metadata. DRAM electrical rails are independent of the logic
initialization voltage. Controller and PHY/interface power are excluded.
The initial electrical DRAM model uses fixed characterization; selecting
`temperature_aware` changes logic leakage and does not imply temperature
scaling of DRAM currents.

The geometry generator writes separate floorplans and `Tflp`/`Tmap` outputs
for `LOGIC_DIE` and `DRAM_DIE`, plus `thermal_manifest.json`. The manifest
records per-die files and flattened element names in the native solver's
bottom-to-top power order: logic followed by DRAM, including residual regions.
The `.stk` declarations use the opposite, top-to-bottom physical order.
Single-die generation also emits a manifest for `TOP_DIE` while retaining
its existing filenames and stack.

The hook configuration accepts `--thermal-manifest` for multi-die operation;
the legacy `--floorplan` and `--temperature-output` pair remains supported
for version 1. Each update appends one combined power row and waits for the
next complete temperature row from every die. Synchronization uses row
indices because 3D-ICE's printed timestamps can round several microsecond
slots to the same value. The offline trace adapter also accepts
`--thermal-manifest`. The runner stages the listed inputs, appends a sentinel
covering every region, reports both dies, and renders separate maps.

The [LLM DRAM3D study](../experiments/llm_dram3d/README.md) exercises the new
SDK workload. The existing single-die decoder remains available separately.

## GVSoC Power-Hook Protocol

GVSoC invokes the configured executable directly:

```text
EXECUTABLE --phase PHASE --request REQUEST --response RESPONSE --config CONFIG
```

Both directions identify:

```json
{"protocol": {"name": "gvsoc-power-hook", "version": 1}}
```

The lifecycle is:

1. `init` at time zero contains no power samples. The hook reads the
   architecture contract and returns every declared path at the initial
   Celsius temperature. A component's optional `voltage_v` is also included
   at initialization; version 1 retains the fallback to
   `metadata.power_voltage_v`. The engine applies the stated fractional
   voltage to that subtree. Version 2 sets logic voltages per component and
   leaves DRAM rails to its electrical model. The field is init-only.
2. `update` follows each complete interval. GVSoC sends average dynamic,
   leakage, and total subtree power in watts plus the temperature used during
   that interval. The response explicitly declares
   `"temperature_unit": "celsius"`.
3. Returned temperatures are recursively applied to each component and its
   descendants. Parent paths are applied before child paths.
4. `final` forwards any remaining partial interval to 3D-ICE. Its response is
   validated and recorded but not applied because simulation has ended.
5. The 3D-ICE hook appends one all-`-1` floorplan slot after `final`, allowing
   the follow-mode server to terminate cleanly.

Requests are atomically replaced. Responses must also be published atomically.
`POWER_HOOK_TRACE_FILE` records all requests as JSON Lines, while
`component_temperatures.csv` records the temperatures produced by 3D-ICE.

The engine-side reference is
`SoftHier/engine/docs/user_manual/power_hook.rst`.

### Expanded SoftHier component mapping

The SoftHier exporter derives areas and paths from the instantiated architecture
and the provider's `power_models/` package. The SDK implementation architecture
has 304 separately coupled domains (19 per cluster) and 320 floorplan regions,
including zero-power `others` residual regions. Scalar and Spatz sources are
siblings, not overlapping power sums. Router/interface paths are mapped into
their corresponding cluster's physical region. HBM/PHY and uncharacterized
local support logic are explicitly listed in contract metadata.

Placement rules live separately in
[`providers/softhier/floorplans/`](providers/softhier/floorplans/README.md).
`SOFTHIER_FLOORPLAN=redmule_strip` retains the original layout (default).
`SOFTHIER_FLOORPLAN=square_bands` selects a square cluster with bottom
RedMulE/TCDM, middle PE-over-Spatz pairs, and top support-component bands.
Band/column dimensions preserve component areas; power paths and mappings do
not change. The rule is recorded in the system contract and run manifest.

The provider stages the runtime and architecture-header generator below
`SOFTHIER_WORKDIR/sdk_snapshot`, preserving SDK checkout headers. Software
defaults to `SOFTHIER_WORKDIR/sw_build_staged`; override `SOFTHIER_SW_BUILD`
for separate kernel builds. `provider.sh build-workload` rebuilds software only
and requires an already compatible hardware-model build. The study harness in
`experiments/component_power/` reuses the SDK `implementation/` generators and
the existing co-simulation/temperature-map renderer, preserving paired binaries
and preloads in each run's `artifacts/` directory.

The provider passes `SIMULATOR_CONFIG` to GVSoC as `SOFTHIER_ARCH_FILE`, so
runtime configuration does not depend on an installed copy of a mutable preset.
After updating this integration, run a full provider build once to install the
architecture-file loader; later compatible kernel builds may be software-only.

The expanded scalar/Spatz domains currently support `SOFTHIER_CORE_MODEL=fast`;
the exporter rejects the accurate-core variant rather than silently omitting
its power. For microsecond floorplan animations, supply `--slot-seconds` to the
existing renderer: 3D-ICE's printed Tflp time may be rounded to milliseconds.
This reconstructs thermal slot timestamps without changing the thermal solver
or the temperature samples. The study harness supplies the actual slot length.

## Replacing or Updating SoftHier

The default integration is intentionally contained in:

- `providers/softhier/provider.sh` for SDK pinning, build, and execution;
- `providers/softhier/export_system_config.py` for architecture inventory and
  exact component mappings;
- `providers/softhier/floorplans/` for selectable cluster placement rules.

The current provider pins SDK branch `chi/soft_hier_dram3d` at commit
`b454aeee4278bb00ecbfcdcc4d1366fe8451d7c8`. Override
`SOFTHIER_SDK_URL` only to use a mirror; override
`SOFTHIER_SDK_COMMIT` deliberately when validating a new SDK revision.
On a fresh clone, `bootstrap` also prepares SystemC `3.0.1` and patched
DRAMSys commit `8565f18b869c26eab712e3bb6494c4d6ae5dd73f` below the
provider work directory. The provider exports `SYSTEMC_HOME` while building
GVSoC and uses the architecture repository's DRAMSys configurations without
modifying the `core` submodule.

Native build stamps include every source submodule's commit and SystemC's
resolved installation path, release version and library hash. The provider
checks the installed SystemC headers and exported library ABI against the
requested release. Changed source modules or native dependencies require a
hardware rebuild; tracked edits in the pinned source checkouts are rejected.

The provider also supports `build-hardware` for SDK applications with their own
build script. `SOFTHIER_BINARY` selects their prebuilt ELF;
`SOFTHIER_INPUT_PRELOAD` adds an input ELF to the common `SIMULATOR_PLATFORM`
preload. `SOFTHIER_PRELOAD_MODE=direct` is the default and consumes no simulated
cycles. `SOFTHIER_RUN_CWD` isolates output dumps, and `SOFTHIER_DRAMSYS_PATH`
selects a private configuration directory. The
[LLM decoder study](../experiments/llm_decode/README.md) exercises these controls.
An explicitly empty `SOFTHIER_CONDA_ENV` preserves an activated Python virtual
environment; the default remains `py312`.

The parent SoftHier repository and its engine submodule both use branch
`chi/power_interface`. When replacing SoftHier, preserve the engine protocol
and provider lifecycle, or update only the SoftHier provider/exporter to match
the new source layout. Generic geometry and 3D-ICE code should not import
SoftHier modules.

For a different simulator, copy the provider directory, implement the actions
above, and select it:

```bash
make coupled-run \
  SIMULATOR_PROVIDER=$PWD/Interface_scripts/providers/my_simulator/provider.sh \
  SIMULATOR_CONFIG=/path/to/config \
  SIMULATOR_APP=/path/to/workload
```

## Tests

```bash
make interface-tests
```

This validates Python compatibility, the system contract, exact
column-to-floorplan mapping, mock `init/update/final` exchanges, Kelvin-to-
Celsius conversion, temperature aggregation, and final sentinel generation.
