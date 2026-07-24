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

## JSON System Contract

The authoritative validator is
[`system_contract.py`](system_contract.py). The current discriminator is
`3dice-cosim-system` version `1`.

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
   Celsius temperature.
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

## Replacing or Updating SoftHier

The default integration is intentionally contained in:

- `providers/softhier/provider.sh` for SDK pinning, build, and execution;
- `providers/softhier/export_system_config.py` for architecture geometry and
  exact component mappings.

The current provider pins SDK commit
`1244fdbc34977aff5a6a10ead079053fb5d31d00`. Override
`SOFTHIER_SDK_URL` only to use a mirror; override
`SOFTHIER_SDK_COMMIT` deliberately when validating a new SDK revision.

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
