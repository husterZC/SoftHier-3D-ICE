# Migration validation

Recorded on 2026-09-20 (UTC). Artifacts are local under
`runs/llm_validation/`; generated runs are ignored by Git. See each run's
`generated/llm/workload.json` for source, ELF, model and configuration hashes.

## Configuration

Validation uses the 4×4 architecture, five scalar cores and four Spatz units per
cluster, 384 KiB TCDM, the provider's `SOFTHIER_CORE_MODEL=fast`, and the staged
SDK and native model corrections documented in this package. Coupled smoke
runs use a 10 µs power interval and a target of 4096 top-die cells.

The SDK revision is `1244fdbc34977aff5a6a10ead079053fb5d31d00`.
The simulator revisions are:

| Repository | Revision |
|---|---|
| SoftHier | `15dcf94bc00494c45c65088f5ed6202509e810eb` |
| engine | `16dadd30d76a0aa2a33a85d3bdc52a75558a83c3` |
| core | `069857b0544832b0d32236caecfa2ce42fd78d4a` |
| pulp | `0ca9415c6856d8e06f722a777002e50ad47d216f` |

## Completed checks

- `make llm-tests`: 20 passed, including real C memory-layout and row-partition
  helpers, invalid configuration rejection, acceptance failure detection, and
  native power-roundoff tests preserving real negative-power failures.
- `make interface-tests`: 43 passed.
- Component-power analysis unit tests: 5 passed.
- Python and provider shell syntax checks passed.
- Existing SDK generated-header hashes were unchanged; simulator source trees
  remained clean. The source workload repository was read only.

| Run directory under `runs/llm_validation/` | Result |
|---|---|
| `accepted-kernel-regression-v3` | Passed real multi-tile GEMM and six-head attention checks |
| `accepted-standalone-prefill` | Passed one-layer, 64-token prefill; every final hidden value finite |
| `accepted-standalone-decode` | Passed two decode steps with a growing synthetic cache; every final hidden value finite |
| `final-prefill-original-v2` | Built the 12-layer, 2048-token preset and all native model variants |
| `final-decode-original-v2` | Built the 12-layer preset with initial cache 2047 and one decode step |

The numerical regression checks every output element of an identity
128×768×768 GEMM spanning multiple M/N/K tiles. It then checks constant-value
attention outputs for all six heads at q=64/kv=64 and q=1/kv=65, with a reserved
cache capacity of 128. This catches stale GEMM accumulation, lost row tails,
incorrect cache head strides and uninitialized NoC reductions after GEMM.

## Coupled smoke acceptance

All four phase/profile combinations passed. Each completed run has successful
simulator and 3D-ICE exits, finite final hidden values, init/update/final hook
phases, contiguous power windows, exact coverage of all 304 components, finite
nonnegative power, finite solved temperatures, and a final termination sentinel.

| Phase | Power profile | Floorplan | Hook exchanges | Simulated duration (ms) | Peak component °C | Result |
|---|---|---|---:|---:|---:|---|
| prefill | `constant` | `redmule_strip` | 103 | 1.014329 | 37.452 | Passed |
| prefill | `temperature_aware` | `redmule_strip` | 103 | 1.014329 | 37.551 | Passed |
| decode | `constant` | `redmule_strip` | 140 | 1.380861 | 47.361 | Passed |
| decode | `temperature_aware` | `square_bands` | 140 | 1.380861 | 45.879 | Passed |

Run directories are `accepted-coupled-<phase>-<profile>` under
`runs/llm_validation/`; the constant decode rerun has the suffix `-v2`.
The first coupled run uses the default phase and preset;
the final decode run selects `square_bands` through the existing root interface.
The runner also confirms 64 Spatz LSU overrides, 2048 patched bank
arbiters, and the patched data NoC in the effective simulator configuration.

Wall time depends on the host. The coupled provider enables tracing and power
accounting and takes substantially longer than standalone execution.

## Power accounting correction

The initial coupled decode run detected a TCDM energy decrease of
`1.4617107808589935e-6 pJ` over a 10 µs idle window, equivalent to approximately
−0.146 pW. A diagnostic engine confirmed this was dynamic-power accumulation
roundoff. The final engine clears negative residues below 1 pW while preserving
positive power and larger negative values. Three tests compile the actual
engine accumulation method before and after the patch to cover these cases.

The prefill thermal runs were completed before this final rounding correction;
the decode reruns exercise the corrected engine. The numerical kernel regression
and standalone results are unaffected because their power accounting is disabled.
Both original presets were rebuilt with all corrected engine variants.

## Scope

The original presets have build validation only; their full runtime has not
been validated. Smoke runs use synthetic identity projection and zero MLP
weights. Finite final hidden values establish execution health, not model
accuracy or trained-model inference quality. Prefill retains full-sequence
attention without a causal mask. Startup initialization is included in the
measured trace, and short smoke temperatures are not steady-state results.

Repeatable commands and output descriptions are in [README.md](README.md).
