#!/usr/bin/env python3
"""Combine four analyzed studies into the full presentation report.

Requires an all-kernel baseline, repeated RMSNorm, and 0.5x/2x SiLU sensitivity
studies. For a single study's report use analyze.py instead. No simulations run.
"""

import argparse
import csv
import json
import math
from pathlib import Path

import analyze as analysis


def read_csv(path):
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def study(path):
    manifest = json.loads((path / "study.json").read_text())
    metrics = {(r["kernel"], r["profile"]): {k: float(v) for k, v in r.items()
               if k not in ("kernel", "profile")} for r in read_csv(path / "metrics.csv")}
    return manifest, metrics, read_csv(path / "component_energy.csv")


def comparison(label, constant, aware):
    # This is an algebraic decomposition of the measured energy, not another
    # thermal run: hold the first, initial-temperature leakage value constant.
    initial_temperature_energy = aware["first_leakage_w"] * aware["duration_us"]
    reference_energy = constant["leakage_energy_uj"]
    return dict(workload=label, duration_us=aware["duration_us"],
        peak_component_constant_c=constant["peak_component_temperature_c"],
        peak_component_aware_c=aware["peak_component_temperature_c"],
        peak_cell_constant_c=constant["peak_cell_temperature_c"],
        peak_cell_aware_c=aware["peak_cell_temperature_c"],
        leakage_energy_increase_pct=100 * (aware["leakage_energy_uj"] / reference_energy - 1),
        total_energy_increase_pct=100 * (aware["total_energy_uj"] / constant["total_energy_uj"] - 1),
        initial_reference_offset_pct=100 * (initial_temperature_energy / reference_energy - 1),
        subsequent_heating_contribution_pct=100 * (aware["leakage_energy_uj"] - initial_temperature_energy) / reference_energy,
        within_run_leakage_growth_pct=aware["leakage_growth_during_run_pct"],
        mean_dynamic_w=aware["average_dynamic_w"],
        mean_leakage_constant_w=constant["average_leakage_w"],
        mean_leakage_aware_w=aware["average_leakage_w"])


def validate_sensitivity(baseline, variant, multiplier):
    """Check that only the requested coefficient envelope changed activity power."""
    for profile in ("constant", "temperature_aware"):
        if baseline[1]["acti", profile]["duration_us"] != variant[1]["acti", profile]["duration_us"]:
            raise ValueError("coefficient scaling changed SiLU execution time")
        left = {r["component"]: r for r in baseline[2] if r["kernel"] == "acti" and r["profile"] == profile}
        right = {r["component"]: r for r in variant[2] if r["kernel"] == "acti" and r["profile"] == profile}
        if left.keys() != right.keys():
            raise ValueError("coefficient scaling changed modeled component types")
        for kind in left:
            factor = 1. if kind in ("memory", "light_redmule") else multiplier
            expected = factor * float(left[kind]["dynamic_energy_uj"])
            if not math.isclose(float(right[kind]["dynamic_energy_uj"]), expected, rel_tol=1e-6, abs_tol=1e-9):
                raise ValueError(f"unexpected dynamic scaling for {kind}, {multiplier}x")
            if profile == "constant":
                expected = factor * float(left[kind]["leakage_energy_uj"])
                if not math.isclose(float(right[kind]["leakage_energy_uj"]), expected, rel_tol=1e-6, abs_tol=1e-9):
                    raise ValueError(f"unexpected constant leakage scaling for {kind}, {multiplier}x")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True,
        help="analyzed result directory with all four kernels, repeats=1 and estimate-scale=1")
    parser.add_argument("--sustained", type=Path, required=True,
        help="analyzed result directory containing norm with repeats>1 and estimate-scale=1")
    parser.add_argument("--low", type=Path, required=True,
        help="analyzed result directory containing acti with repeats=1 and estimate-scale=0.5")
    parser.add_argument("--high", type=Path, required=True,
        help="analyzed result directory containing acti with repeats=1 and estimate-scale=2")
    parser.add_argument("--output", type=Path, required=True,
        help="summary directory; replaces same-named generated files and includes timing.csv if present")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    baseline, sustained, low, high = [study(p) for p in (args.baseline, args.sustained, args.low, args.high)]
    for data, scale in ((baseline, 1.), (sustained, 1.), (low, .5), (high, 2.)):
        if data[0]["estimate_scale"] != scale:
            raise ValueError("unexpected coefficient multiplier in manifest")
        if data[0]["target_cells"] != baseline[0]["target_cells"]:
            raise ValueError("studies use different thermal meshes")
        if data[0].get("floorplan_rule", "redmule_strip") != baseline[0].get("floorplan_rule", "redmule_strip"):
            raise ValueError("coefficient studies must use the same floorplan rule")
    if any(data[0]["repeats"] != 1 for data in (baseline, low, high)) or sustained[0]["repeats"] <= 1:
        raise ValueError("expected single-invocation baseline/envelopes and a repeated RMSNorm study")
    validate_sensitivity(baseline, low, .5)
    validate_sensitivity(baseline, high, 2.)
    rows = [comparison(analysis.NAMES[k], baseline[1][k, "constant"], baseline[1][k, "temperature_aware"])
            for k in ("norm", "acti", "gemm", "attn")]
    repeats = sustained[0]["repeats"]
    rows.append(comparison(f"RMSNorm ×{repeats}", sustained[1]["norm", "constant"], sustained[1]["norm", "temperature_aware"]))
    envelope = [dict(multiplier=scale, **comparison("SiLU", data[1]["acti", "constant"], data[1]["acti", "temperature_aware"]))
                for data, scale in ((low, .5), (baseline, 1.), (high, 2.))]
    analysis.csv_file(args.output / "comparison.csv", rows)
    analysis.csv_file(args.output / "sensitivity.csv", envelope)

    analysis.BASE.configure_style()
    plt, np = analysis.plt, analysis.np
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.3), layout="constrained")
    x = np.arange(len(rows))
    initial = np.array([r["initial_reference_offset_pct"] for r in rows])
    feedback = np.array([r["subsequent_heating_contribution_pct"] for r in rows])
    axes[0].bar(x, initial, color="#56B4E9", label="Initial 26.85 °C vs 25 °C reference")
    axes[0].bar(x, feedback, bottom=initial, color="#D55E00", label="Subsequent temperature feedback")
    axes[0].set(ylabel="Leakage-energy increase (% of constant)", title="Separating reference offset from self-heating")
    axes[0].set_xticks(x, [r["workload"].replace(" ", "\n", 1) for r in rows])
    axes[0].legend(fontsize=8)
    scales = [r["multiplier"] for r in envelope]
    for field, color, label in (("mean_dynamic_w", "#0072B2", "Mean dynamic"),
                               ("mean_leakage_aware_w", "#D55E00", "Mean leakage, temperature-aware")):
        axes[1].plot(scales, [r[field] for r in envelope], "o-", color=color, label=label)
    axes[1].set(xlabel="New-logic coefficient multiplier", ylabel="Mean modeled power (W)",
                title="SiLU estimate sensitivity; RedMulE/SRAM fixed", xticks=scales)
    axes[1].legend(fontsize=8)
    analysis.BASE.save_figure(fig, args.output, "feedback_and_sensitivity")

    table = "\n".join(f"| {r['workload']} | {r['duration_us']:.3f} | {r['peak_component_constant_c']:.3f} → {r['peak_component_aware_c']:.3f} | {r['leakage_energy_increase_pct']:.2f} | {r['total_energy_increase_pct']:.2f} | {r['within_run_leakage_growth_pct']:.2f} |" for r in rows)
    sensitivity_table = "\n".join(f"| {r['multiplier']:g}× | {r['mean_dynamic_w']:.4f} | {r['mean_leakage_aware_w']:.4f} | {r['total_energy_increase_pct']:.2f} |" for r in envelope)
    attn = rows[3]
    c = baseline[1]["attn", "constant"]
    a = baseline[1]["attn", "temperature_aware"]
    # Reports live one directory above the per-study results by default.
    import os
    main_dir = os.path.relpath(args.baseline, args.output)
    sustained_dir = os.path.relpath(args.sustained, args.output)
    models_readme = os.path.relpath(
        analysis.ROOT / "SoftHier/pulp/pulp/chips/soft_hier_old/power_models/README.md", args.output)
    study_readme = os.path.relpath(Path(__file__).resolve().parent / "README.md", args.output)
    floorplan_rule = baseline[0].get("floorplan_rule", "redmule_strip")
    validation = [row for folder in (args.baseline, args.sustained, args.low, args.high)
                  for row in read_csv(folder / "validation.csv")]
    maximum_dynamic_error = max(float(r["max_dynamic_pair_difference_w"]) for r in validation)
    maximum_leakage_error = max(float(r["max_logic_leakage_model_error_w"]) for r in validation)
    timing_note = ""
    if (args.output / "timing.csv").exists():
        timing_rows = read_csv(args.output / "timing.csv")
        if any(float(row["difference_ns"]) != 0 for row in timing_rows):
            raise ValueError("uncoupled timing control failed")
        timing_note = "\nPower/thermal-disabled controls also match the coupled completion times: " + ", ".join(
            f"{analysis.NAMES[r['kernel']]} {int(r['coupled_ns']):,} ns" for r in timing_rows) + ". See [timing checks](timing.csv).\n"
    report = f"""# Activity-aware thermal co-simulation of SoftHier implementation kernels

## Abstract

We extend SoftHier's existing runtime GVSoC–3D-ICE feedback loop with estimated
power models for scalar cores, Spatz, iDMA, and the data/synchronization NoCs.
A coarse transpose-engine model completes the FlatAttention execution path.
The updated area-based floorplan maps 304 non-overlapping power domains onto
320 regions. Four SDK `implementation/` kernels and a repeated RMSNorm workload
are compared using fixed and temperature-dependent leakage. In FlatAttention,
leakage energy increases by {attn['leakage_energy_increase_pct']:.2f}% and total modeled energy by
{attn['total_energy_increase_pct']:.2f}%; leakage rises by {attn['within_run_leakage_growth_pct']:.2f}% within the temperature-aware run.
These are illustrative model predictions, not calibrated silicon measurements.

## 1. Model and experimental method

The implementation configuration comprises 16 clusters, each containing five
scalar cores, four Spatz units, a 32×16 RedMulE, 384 KiB TCDM, 64 KiB instruction
SRAM, 128 KiB stack SRAM, one iDMA, one transpose engine, and two router/NI pairs.
Scalar and vector domains are siblings for power aggregation, so their thermal
inputs do not double count a shared component subtree. NoC activity follows
actual request routes, with read payloads accounted on return and collective
branches accounted separately. Memory traffic remains in its own domain.

Spatz FP64 arithmetic and VRF reference energies follow
[Spatz, Figs. 9–12](https://arxiv.org/pdf/2309.10137v2). DMA structural scaling
is informed by [iDMA, Section IV](https://arxiv.org/html/2305.05240v2); its
per-byte energy is an explicit estimate. Router energy uses approximately
0.15 pJ/byte/hop from [FlooNoC, Section VI-D](https://arxiv.org/pdf/2409.17606v2).
Scalar, low-precision, custom-operation, transpose, leakage and technology
projection coefficients are engineering assumptions. The complete versioned
tables, units and accounting boundaries are documented in
[power_models]({models_readme}).

At the study's nominal 5 nm, 0.7 V and 1 GHz, logic/SRAM leakage follows sampled
exponentials with doubling intervals of 22/27 °C. GVSoC linearly interpolates
the samples and clamps beyond their temperature range. Only `constant` and
`temperature_aware` profiles are used; both retain identical dynamic models.
Activity energy is charged per instruction, active vector element, DMA byte,
or NoC byte/hop; residual clocks and leakage remain when activity is absent.

The existing floorplan generation, thermal mapping, feedback hook, 3D-ICE stack,
and animation renderer are reused. All studies use the `{floorplan_rule}` layout
with {baseline[0]['target_cells']:,} requested source-layer cells; the actual count
depends on floorplan discretization. Coupling intervals are 1 µs for single RMSNorm, SiLU,
and SUMMA GEMM; 20 µs for FlatAttention; and 10 µs for RMSNorm ×{repeats}.
Thermal integration steps are one tenth of those intervals. Each pair reuses
the same ELF and preload, geometry, time windows and dynamic power. No DVFS or
temperature-dependent delay model is introduced.

The SDK's unchanged FP16 kernel presets are used:

| Kernel | Configuration |
|---|---|
| RMSNorm | 512 × 512 input |
| SiLU | 512 × 512 input; gate and bias disabled |
| SUMMA GEMM | M = N = K = 512; 128 × 128 × 128 tiles; one 4 × 4 cluster group |
| FlatAttention | Q/KV sequence lengths 512; head dimension 128; 32 heads and groups; batch 1; asynchronous flattening enabled |

Duration and energy cover the full simulator invocation, including startup
and termination, not just the kernel's internal performance-counter region.
The repeated RMSNorm case reinitializes its execution descriptor every time
and inserts an inter-repetition global barrier; it is not a separate cold boot
per repetition. Preload data are reused.

## 2. Results

| Workload | Duration (µs) | Peak component-average T, constant → aware (°C) | Leakage energy Δ (%) | Total energy Δ (%) | Within-run leakage rise (%) |
|---|---:|---:|---:|---:|---:|
{table}

For FlatAttention, average dynamic power is {a['average_dynamic_w']:.3f} W in both profiles.
Mean leakage increases from {c['average_leakage_w']:.4f} to {a['average_leakage_w']:.4f} W;
total modeled energy increases from {c['total_energy_uj']:.2f} to {a['total_energy_uj']:.2f} µJ.
Peak finite-volume cell temperature is {c['peak_cell_temperature_c']:.3f} °C versus
{a['peak_cell_temperature_c']:.3f} °C. These cell peaks differ from the floorplan
averages in the table. The modest temperature difference of
{attn['peak_component_aware_c'] - attn['peak_component_constant_c']:.3f} °C at the hottest component
does not imply an equally small relative change in leakage.

![Component power and paired energy impact]({main_dir}/workload_summary.png)

![FlatAttention: leakage and temperature versus simulation time]({main_dir}/attn_leakage_temperature_time.png)

The requested dual-axis plots put **time on the x-axis**, leakage on the left
y-axis, and temperature on the right. Each panel selects the hottest instance
of one component type; axis scaling is independent and does not imply equal units.
Stepwise power is the interval average; temperature is the returned endpoint.

## 3. Reference offset, self-heating, and coefficient sensitivity

The constant control uses leakage at 25 °C, while the thermal stack starts at
26.85 °C. Thus the A/B energy difference includes an initial reference offset.
We separate it algebraically: initial-offset energy is
`P_leak(T_initial) × duration − E_leak_constant`; the remaining contribution is
`E_leak_aware − P_leak(T_initial) × duration`. The latter measures leakage above
the initial-temperature level, not a second controlled thermal simulation.
For FlatAttention these contributions are {attn['initial_reference_offset_pct']:.2f} and
{attn['subsequent_heating_contribution_pct']:.2f} percentage points of the constant leakage energy.
Consequently, its energy difference cannot be attributed solely to the initial
reference offset. For the short vector kernels, most of the difference can.

![Reference-offset decomposition and coefficient sensitivity](feedback_and_sensitivity.png)

The sensitivity experiment multiplies the new logic models' dynamic and leakage
coefficients together by 0.5 or 2. RedMulE, SRAM, floorplan area and workload are
unchanged. These cases are assumption envelopes, **not confidence intervals**.
Per-kind dynamic energy and constant leakage are checked to scale as requested;
simulated execution time remains unchanged.

| New-logic multiplier | SiLU mean dynamic (W) | SiLU mean aware leakage (W) | Total A/B energy Δ (%) |
|---|---:|---:|---:|
{sensitivity_table}

## 4. Thermal maps and presentation assets

![Expanded representative cluster floorplan]({main_dir}/cluster_floorplan.png)

![FlatAttention paired final thermal maps]({main_dir}/attn_thermal_maps.png)

The full-cell animations use the existing renderer and preserve a common color
scale within each constant/aware pair. They resolve gradients hidden by
component averaging. Useful slide assets are:

- [FlatAttention full-cell animation, temperature-aware]({main_dir}/attn_temperature_aware_cells.gif)
- [FlatAttention full-cell animation, constant]({main_dir}/attn_constant_cells.gif)
- [RMSNorm ×{repeats}, temperature-aware]({sustained_dir}/norm_temperature_aware.gif)
- [FlatAttention leakage/temperature curves, vector PDF]({main_dir}/attn_leakage_temperature_time.pdf)
- [Summary power/energy figure, vector PDF]({main_dir}/workload_summary.pdf)
- [Coefficient assumptions]({main_dir}/model_assumptions.pdf)
- [Comparison numbers](comparison.csv) and [sensitivity numbers](sensitivity.csv)

Every static figure is available as PNG and vector PDF. Per-study `metrics.csv`,
`component_energy.csv`, `timeseries.csv` and `validation.csv` retain numeric data
and checks. The component-energy file separates new-logic activity from residual
clocks; a nonzero dynamic total alone is not taken as evidence of execution.
`study.json` records raw run locations and artifact hashes.

## 5. Validation and limitations

The paired runs pass equal-binary/preload, equal-duration, identical-floorplan,
per-window dynamic-power, constant-leakage and live temperature-table checks.
Across {len(validation)} completed runs, the maximum paired dynamic-power discrepancy
is {maximum_dynamic_error:.3g} W; the maximum new-logic leakage-table residual is
{maximum_leakage_error:.3g} W.
All new component types show event energy in FlatAttention, including transpose.
SDK runtime headers and the tracked default architecture are not overwritten.
{timing_note}
The SDK preload scripts contain placeholder timing data; these runs establish
execution and activity behavior, not numerical kernel correctness.

These are transient, cold-start studies. No steady-state, thermal runaway,
calibrated absolute accuracy, or mesh/coupling convergence claim is made. The
existing thermal solver advances a final partial power window as a full slot;
energy integration uses the exact window, while its temperature endpoint is
quantized by at most one coupling interval. Tflp output is rounded to about
0.001 K, so the smallest temperature differences are at its reporting precision.
HBM/PHY, uncharacterized local interconnect and support logic are outside the
power boundary. The area-based floorplan is not placement-and-routing output.

## 6. Reproducibility

See [study instructions]({study_readme}) for build, run and analysis commands.
Only validated clean pairs are used; superseded smoke trials and interrupted
runs are excluded. No 3D-ICE solver or SDK source modifications are required.
The source changes span the root interface plus SoftHier's `engine`, `core`
and `pulp` submodules. Nothing has been committed or pushed by this study.
"""
    (args.output / "report.md").write_text(report)
    print(f"Wrote presentation report and coefficient-scaling checks: {args.output}")


if __name__ == "__main__":
    main()
