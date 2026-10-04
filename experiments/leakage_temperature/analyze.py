#!/usr/bin/env python3
"""Compare constant and temperature-aware SoftHier leakage co-simulations.

The script consumes authoritative run-local JSONL/CSV traces, integrates power
over the exact GVSoC windows, and emits machine-readable metrics, slide-ready
figures (PNG + vector PDF), and a self-contained academic-style Markdown report.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


os.environ.setdefault("MPLCONFIGDIR", "/tmp/softhier-leakage-matplotlib")
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402


CONSTANT_COLOR = "#0072B2"
TEMPERATURE_COLOR = "#D55E00"
AMBIENT_C = 26.85
ACTIVE_REDMULE = "/chip/cluster_0/redmule"
ACTIVE_TCDM = "/chip/cluster_0/tcdm"
DEFAULT_REDMULE_KGE = 100.0 + 128.0 * 32.0 * 8.59
DEFAULT_TCDM_MODELED_BYTES = 64 * (1_048_576 // 64 + 8) + 128
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def portable_path(path: Path | str) -> str:
    """Return a repository-relative path when possible."""

    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(resolved)


@dataclass
class RunData:
    path: Path
    profile: str
    records: list[dict[str, Any]]
    temperatures: list[dict[str, Any]]
    summary: dict[str, str]
    metadata: dict[str, Any]

    @property
    def power_records(self) -> list[dict[str, Any]]:
        return [record for record in self.records if record["components"]]

    @property
    def duration_ps(self) -> int:
        return max(record["window"]["end_ps"] for record in self.records)

    @property
    def duration_s(self) -> float:
        return self.duration_ps * 1.0e-12


def parse_summary(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
    return values


def load_run(path: Path) -> RunData:
    path = path.resolve()
    required = (
        path / "summary.txt",
        path / "generated" / "system_config.json",
        path / "traces" / "power_hook_trace.jsonl",
        path / "traces" / "component_temperatures.csv",
    )
    missing = [str(item) for item in required if not item.is_file()]
    if missing:
        raise ValueError("run is incomplete; missing: " + ", ".join(missing))

    summary = parse_summary(required[0])
    if summary.get("simulator") != "0" or summary.get("3dice_server") != "0":
        raise ValueError(f"run did not complete successfully: {path}")

    contract = json.loads(required[1].read_text(encoding="utf-8"))
    metadata = contract.get("metadata", {})
    profile = metadata.get("power_model_profile")
    if not profile:
        profile = summary.get("softhier_power_profile", "constant")

    records = []
    with required[2].open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"invalid JSONL record {line_number} in {required[2]}"
                ) from error

    temperatures = []
    with required[3].open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            temperatures.append(
                {
                    "phase": row["phase"],
                    "start_ps": int(row["start_ps"]),
                    "end_ps": int(row["end_ps"]),
                    "path": row["component_path"],
                    "power_w": float(row["power_w"]) if row["power_w"] else None,
                    "temperature_c": float(row["temperature_c"]),
                }
            )

    run = RunData(path, str(profile), records, temperatures, summary, metadata)
    validate_run(run)
    return run


def validate_run(run: RunData) -> None:
    phases = [record["phase"] for record in run.records]
    if not phases or phases[0] != "init" or phases[-1] != "final":
        raise ValueError(f"invalid hook lifecycle in {run.path}")

    previous_end = 0
    component_paths = None
    for record in run.power_records:
        window = record["window"]
        if window["start_ps"] != previous_end or window["end_ps"] <= previous_end:
            raise ValueError(f"non-contiguous power windows in {run.path}")
        previous_end = window["end_ps"]
        paths = tuple(component["path"] for component in record["components"])
        if component_paths is None:
            component_paths = paths
        elif paths != component_paths:
            raise ValueError(f"component order changed within {run.path}")
        for component in record["components"]:
            expected = component["dynamic_w"] + component["leakage_w"]
            if not math.isclose(component["total_w"], expected, rel_tol=1e-9, abs_tol=1e-12):
                raise ValueError(f"inconsistent component power in {run.path}")

    expected_temperature_rows = len(run.records) * len(component_paths or ())
    if len(run.temperatures) != expected_temperature_rows:
        raise ValueError(
            f"temperature row count mismatch in {run.path}: "
            f"got {len(run.temperatures)}, expected {expected_temperature_rows}"
        )


def power_series(run: RunData, field: str, path: str | None = None) -> tuple[np.ndarray, np.ndarray]:
    times = []
    values = []
    for record in run.power_records:
        times.append(record["window"]["end_ps"] * 1.0e-9)
        selected = record["components"]
        if path is not None:
            selected = [component for component in selected if component["path"] == path]
            if len(selected) != 1:
                raise ValueError(f"missing component {path} in {run.path}")
        values.append(sum(float(component[field]) for component in selected))
    return np.asarray(times), np.asarray(values)


def temperature_series(run: RunData, path: str | None = None) -> tuple[np.ndarray, np.ndarray]:
    if path is not None:
        selected = [row for row in run.temperatures if row["path"] == path]
        return (
            np.asarray([row["end_ps"] * 1.0e-9 for row in selected]),
            np.asarray([row["temperature_c"] for row in selected]),
        )

    by_time: dict[int, list[float]] = defaultdict(list)
    for row in run.temperatures:
        by_time[row["end_ps"]].append(row["temperature_c"])
    times = np.asarray(sorted(by_time))
    maxima = np.asarray([max(by_time[int(time)]) for time in times])
    return times * 1.0e-9, maxima


def component_temperature_used_series(run: RunData, path: str) -> tuple[np.ndarray, np.ndarray]:
    temperatures = []
    leakages = []
    for record in run.power_records:
        selected = [component for component in record["components"] if component["path"] == path]
        if len(selected) != 1:
            raise ValueError(f"missing component {path} in {run.path}")
        temperatures.append(selected[0]["temperature_c"])
        leakages.append(selected[0]["leakage_w"])
    return np.asarray(temperatures), np.asarray(leakages)


def integrate_energy(run: RunData, field: str, kind: str | None = None) -> float:
    energy_j = 0.0
    suffix = f"/{kind}" if kind else None
    for record in run.power_records:
        duration_s = (
            record["window"]["end_ps"] - record["window"]["start_ps"]
        ) * 1.0e-12
        components = record["components"]
        if suffix:
            components = [component for component in components if component["path"].endswith(suffix)]
        energy_j += sum(float(component[field]) for component in components) * duration_s
    return energy_j


def metrics(run: RunData) -> dict[str, float]:
    _, max_temperature = temperature_series(run)
    _, active_temperature = temperature_series(run, ACTIVE_REDMULE)
    _, active_tcdm_temperature = temperature_series(run, ACTIVE_TCDM)
    _, chip_leakage = power_series(run, "leakage_w")
    _, active_leakage = power_series(run, "leakage_w", ACTIVE_REDMULE)
    _, tcdm_leakage = power_series(run, "leakage_w", ACTIVE_TCDM)
    dynamic_j = integrate_energy(run, "dynamic_w")
    leakage_j = integrate_energy(run, "leakage_w")
    redmule_leakage_j = integrate_energy(run, "leakage_w", "redmule")
    tcdm_leakage_j = integrate_energy(run, "leakage_w", "tcdm")
    total_j = dynamic_j + leakage_j
    return {
        "simulated_time_ms": run.duration_s * 1.0e3,
        "thermal_exchanges": float(len(run.records)),
        "peak_temperature_c": float(np.max(max_temperature)),
        "peak_temperature_rise_c": float(np.max(max_temperature) - AMBIENT_C),
        "active_redmule_peak_temperature_c": float(np.max(active_temperature)),
        "active_tcdm_peak_temperature_c": float(np.max(active_tcdm_temperature)),
        "chip_average_leakage_w": leakage_j / run.duration_s,
        "chip_peak_leakage_w": float(np.max(chip_leakage)),
        "active_redmule_peak_leakage_w": float(np.max(active_leakage)),
        "active_tcdm_peak_leakage_w": float(np.max(tcdm_leakage)),
        "dynamic_energy_mj": dynamic_j * 1.0e3,
        "leakage_energy_mj": leakage_j * 1.0e3,
        "redmule_leakage_energy_mj": redmule_leakage_j * 1.0e3,
        "tcdm_leakage_energy_mj": tcdm_leakage_j * 1.0e3,
        "total_energy_mj": total_j * 1.0e3,
        "leakage_energy_fraction_pct": 100.0 * leakage_j / total_j,
    }


UNITS = {
    "simulated_time_ms": "ms",
    "thermal_exchanges": "count",
    "peak_temperature_c": "°C",
    "peak_temperature_rise_c": "°C",
    "active_redmule_peak_temperature_c": "°C",
    "active_tcdm_peak_temperature_c": "°C",
    "chip_average_leakage_w": "W",
    "chip_peak_leakage_w": "W",
    "active_redmule_peak_leakage_w": "W",
    "active_tcdm_peak_leakage_w": "W",
    "dynamic_energy_mj": "mJ",
    "leakage_energy_mj": "mJ",
    "redmule_leakage_energy_mj": "mJ",
    "tcdm_leakage_energy_mj": "mJ",
    "total_energy_mj": "mJ",
    "leakage_energy_fraction_pct": "%",
}


def configure_style() -> None:
    plt.rcParams.update(
        {
            "font.family": "serif",
            "font.serif": ["DejaVu Serif"],
            "mathtext.fontset": "dejavuserif",
            "font.size": 10.5,
            "axes.titlesize": 11.5,
            "axes.labelsize": 10.5,
            "legend.fontsize": 9.2,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.alpha": 0.24,
            "grid.linewidth": 0.7,
            "figure.dpi": 120,
            "savefig.dpi": 320,
            "savefig.bbox": "tight",
        }
    )


def save_figure(fig: plt.Figure, output_dir: Path, stem: str) -> None:
    fig.savefig(output_dir / f"{stem}.png")
    fig.savefig(output_dir / f"{stem}.pdf")
    plt.close(fig)


def load_component_model_module():
    model_path = (
        PROJECT_ROOT
        / "SoftHier"
        / "pulp"
        / "pulp"
        / "chips"
        / "soft_hier_old"
        / "power_models"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location("study_power_models", model_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load component power models from {model_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_model_tables(path: Path) -> None:
    models = load_component_model_module()
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(
            (
                "technology_node",
                "component",
                "profile",
                "temperature_c",
                "voltage_v",
                "leakage_w_default_design",
            )
        )
        for node in ("22nm", "12nm", "7nm", "5nm"):
            for profile in ("constant", "temperature_aware"):
                sources = {
                    "light_redmule": models.light_redmule_power_source(
                        num_tile_mac=4_194_304,
                        redmule_kge=DEFAULT_REDMULE_KGE,
                        tech_node=node,
                        profile=profile,
                    )["leakage"],
                    "tcdm": models.memory_power_sources(
                        size_bytes=DEFAULT_TCDM_MODELED_BYTES,
                        tech_node=node,
                        profile=profile,
                    )["background"]["leakage"],
                }
                for component, table in sources.items():
                    for temperature, voltage_table in table["values"].items():
                        for voltage, frequency_table in voltage_table.items():
                            writer.writerow(
                                (
                                    node,
                                    component,
                                    profile,
                                    temperature,
                                    voltage,
                                    frequency_table["any"],
                                )
                            )


def plot_model_scaling(output_dir: Path) -> None:
    models = load_component_model_module()
    nodes = ("22nm", "12nm", "7nm", "5nm")
    redmule_reference = []
    memory_reference = []
    redmule_doubling = []
    memory_doubling = []
    for node in nodes:
        redmule = models.light_redmule_power_source(
            num_tile_mac=4_194_304,
            redmule_kge=DEFAULT_REDMULE_KGE,
            tech_node=node,
            profile="constant",
        )["leakage"]["values"]["25"]
        memory = models.memory_power_sources(
            size_bytes=DEFAULT_TCDM_MODELED_BYTES,
            tech_node=node,
            profile="constant",
        )["background"]["leakage"]["values"]["25"]
        redmule_reference.append(max(value["any"] for value in redmule.values()))
        memory_reference.append(1.0e3 * max(value["any"] for value in memory.values()))
        redmule_doubling.append(
            models.component_model_metadata("light_redmule", node)[
                "leakage_doubling_temperature_c"
            ]
        )
        memory_doubling.append(
            models.component_model_metadata("memory", node)[
                "leakage_doubling_temperature_c"
            ]
        )

    fig, axes = plt.subplots(1, 2, figsize=(9.2, 3.8))
    x = np.arange(len(nodes))
    width = 0.36
    axes[0].bar(x - width / 2, redmule_reference, width, color="#009E73", label="LightRedMulE (W)")
    memory_axis = axes[0].twinx()
    memory_axis.bar(x + width / 2, memory_reference, width, color="#CC79A7", label="TCDM (mW)")
    axes[0].set_xticks(x, nodes)
    axes[0].set_ylabel("LightRedMulE leakage (W)")
    memory_axis.set_ylabel("TCDM leakage (mW)")
    axes[0].set_title("(a) 25 °C reference for default design")
    handles_a, labels_a = axes[0].get_legend_handles_labels()
    handles_b, labels_b = memory_axis.get_legend_handles_labels()
    axes[0].legend(handles_a + handles_b, labels_a + labels_b, frameon=False, loc="upper center")
    memory_axis.grid(False)

    axes[1].plot(nodes, redmule_doubling, marker="o", linewidth=2, color="#009E73", label="LightRedMulE")
    axes[1].plot(nodes, memory_doubling, marker="s", linewidth=2, color="#CC79A7", label="TCDM")
    axes[1].set_ylabel("Leakage doubling temperature (°C)")
    axes[1].set_title("(b) Temperature sensitivity")
    axes[1].legend(frameon=False)
    axes[1].invert_yaxis()
    fig.suptitle("Technology-scaled leakage assumptions", fontweight="bold", y=1.02)
    fig.tight_layout()
    save_figure(fig, output_dir, "figure_model_scaling")


def plot_temperature(constant: RunData, aware: RunData, output_dir: Path) -> None:
    fig, axes = plt.subplots(2, 1, figsize=(7.2, 6.1), sharex=True)
    for run, label, color in (
        (constant, "Constant-reference leakage", CONSTANT_COLOR),
        (aware, "Temperature-aware leakage", TEMPERATURE_COLOR),
    ):
        time, temperature = temperature_series(run, ACTIVE_REDMULE)
        axes[0].plot(time, temperature, color=color, linewidth=2.0, label=label)

    axes[0].set_ylabel("Temperature (°C)")
    axes[0].set_title("(a) Hotspot: active cluster-0 LightRedMulE")
    axes[0].legend(frameon=False, loc="lower right")

    constant_time, constant_active = temperature_series(constant, ACTIVE_REDMULE)
    aware_time, aware_active = temperature_series(aware, ACTIVE_REDMULE)
    constant_tcdm_time, constant_tcdm = temperature_series(constant, ACTIVE_TCDM)
    aware_tcdm_time, aware_tcdm = temperature_series(aware, ACTIVE_TCDM)
    if not (
        np.array_equal(constant_time, aware_time)
        and np.array_equal(constant_tcdm_time, aware_tcdm_time)
    ):
        raise ValueError("A/B temperature samples are not time-aligned")
    axes[1].plot(
        aware_time,
        aware_active - constant_active,
        color=TEMPERATURE_COLOR,
        linewidth=2.0,
        label="Active LightRedMulE",
    )
    axes[1].plot(
        aware_tcdm_time,
        aware_tcdm - constant_tcdm,
        color="#009E73",
        linewidth=1.7,
        linestyle="--",
        label="Active TCDM",
    )
    axes[1].axhline(0.0, color=CONSTANT_COLOR, linewidth=1.0, alpha=0.8)
    axes[1].set_ylabel("Temperature increase, ΔT (°C)")
    axes[1].set_xlabel("Simulated time (ms)")
    axes[1].set_title("(b) Additional heating caused by leakage feedback")
    axes[1].legend(frameon=False, loc="lower right")
    fig.suptitle("Closed-loop temperature response", fontweight="bold", y=1.01)
    fig.tight_layout()
    save_figure(fig, output_dir, "figure_temperature_comparison")


def plot_power_energy(
    constant: RunData,
    aware: RunData,
    constant_metrics: dict[str, float],
    aware_metrics: dict[str, float],
    output_dir: Path,
) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(9.2, 3.8))
    for run, label, color in (
        (constant, "Constant", CONSTANT_COLOR),
        (aware, "Temperature-aware", TEMPERATURE_COLOR),
    ):
        time, leakage = power_series(run, "leakage_w")
        axes[0].step(time, leakage, where="post", color=color, linewidth=1.9, label=label)
    axes[0].set_xlabel("Simulated time (ms)")
    axes[0].set_ylabel("Aggregate leakage power (W)")
    axes[0].set_title("(a) Chip-level modeled leakage")
    axes[0].legend(frameon=False)

    labels = ["Constant", "Temperature-aware"]
    dynamic = [constant_metrics["dynamic_energy_mj"], aware_metrics["dynamic_energy_mj"]]
    leakage = [constant_metrics["leakage_energy_mj"], aware_metrics["leakage_energy_mj"]]
    x = np.arange(2)
    axes[1].bar(x, dynamic, width=0.62, color="#999999", label="Dynamic")
    axes[1].bar(x, leakage, width=0.62, bottom=dynamic, color="#CC79A7", label="Leakage")
    axes[1].set_xticks(x, labels)
    axes[1].set_ylabel("Integrated energy (mJ)")
    axes[1].set_title("(b) Energy over the full application")
    for index, (dyn, leak) in enumerate(zip(dynamic, leakage)):
        axes[1].text(
            index,
            dyn / 2.0,
            f"Dynamic\n{dyn:.1f} mJ",
            ha="center",
            va="center",
            color="white",
            fontweight="bold",
        )
        axes[1].text(
            index,
            dyn + leak / 2.0,
            f"Leakage\n{leak:.1f} mJ",
            ha="center",
            va="center",
            color="white",
            fontweight="bold",
        )
        axes[1].text(index, dyn + leak, f"{dyn + leak:.1f} mJ", ha="center", va="bottom")

    fig.suptitle("Power and energy impact of leakage feedback", fontweight="bold", y=1.02)
    fig.tight_layout()
    save_figure(fig, output_dir, "figure_power_energy_comparison")


def plot_characteristic(constant: RunData, aware: RunData, output_dir: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(9.2, 3.8))
    for axis, path, title, scale, unit in (
        (axes[0], ACTIVE_REDMULE, "(a) Cluster-0 LightRedMulE", 1.0, "W"),
        (axes[1], ACTIVE_TCDM, "(b) Cluster-0 TCDM", 1.0e3, "mW"),
    ):
        for run, label, color in (
            (constant, "Constant", CONSTANT_COLOR),
            (aware, "Temperature-aware", TEMPERATURE_COLOR),
        ):
            temperature, leakage = component_temperature_used_series(run, path)
            order = np.argsort(temperature)
            axis.plot(
                temperature[order],
                leakage[order] * scale,
                color=color,
                linewidth=1.7,
                marker="o",
                markersize=2.4,
                markevery=max(1, len(order) // 14),
                label=label,
            )
        axis.set_xlabel("Applied component temperature (°C)")
        axis.set_ylabel(f"Leakage power ({unit})")
        axis.set_title(title)
    axes[0].legend(frameon=False)
    fig.suptitle("Runtime leakage-table response", fontweight="bold", y=1.02)
    fig.tight_layout()
    save_figure(fig, output_dir, "figure_leakage_characteristic")


def plot_temperature_leakage_trajectory(aware: RunData, output_dir: Path) -> None:
    """Plot treatment temperature and leakage histories against simulation time."""

    paths = (
        (ACTIVE_REDMULE, "(a) Cluster-0 LightRedMulE", 1.0, "W", 3),
        (ACTIVE_TCDM, "(b) Cluster-0 TCDM", 1.0e3, "mW", 1),
    )
    temperature_color = TEMPERATURE_COLOR
    leakage_color = "#6A3D9A"
    fig, axes = plt.subplots(1, 2, figsize=(9.2, 4.15), constrained_layout=True)
    for axis, (path, title, scale, unit, digits) in zip(axes, paths):
        times, trace_leakage = power_series(aware, "leakage_w", path)
        temperatures, source_leakage = component_temperature_used_series(aware, path)
        if not (
            len(times) == len(temperatures)
            and np.allclose(trace_leakage, source_leakage, rtol=1.0e-12, atol=1.0e-15)
        ):
            raise ValueError(f"time/power sample mismatch for {path} in {aware.path}")

        scaled_leakage = trace_leakage * scale
        temperature_line = axis.plot(
            times,
            temperatures,
            color=temperature_color,
            linewidth=2.2,
            label="Temperature",
        )[0]
        axis.set_xlabel("Simulated time (ms)")
        axis.set_ylabel("Temperature (°C)", color=temperature_color)
        axis.tick_params(axis="y", labelcolor=temperature_color)
        axis.set_title(title)

        leakage_axis = axis.twinx()
        leakage_line = leakage_axis.plot(
            times,
            scaled_leakage,
            color=leakage_color,
            linewidth=2.0,
            linestyle="--",
            label="Leakage power",
        )[0]
        leakage_axis.set_ylabel(f"Leakage power ({unit})", color=leakage_color)
        leakage_axis.tick_params(axis="y", labelcolor=leakage_color)
        leakage_axis.grid(False)
        axis.legend(
            (temperature_line, leakage_line),
            (temperature_line.get_label(), leakage_line.get_label()),
            frameon=False,
            loc="lower right",
        )
        axis.text(
            0.97,
            0.18,
            f"Final: {temperatures[-1]:.2f} °C\n"
            f"{scaled_leakage[-1]:.{digits}f} {unit}",
            transform=axis.transAxes,
            ha="right",
            va="bottom",
            fontsize=9.0,
            bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.78},
        )

    fig.suptitle(
        "Temperature-aware temperature and leakage over time",
        fontweight="bold",
    )
    save_figure(fig, output_dir, "figure_temperature_leakage_trajectory")


def plot_normalized_effect(
    constant_metrics: dict[str, float],
    aware_metrics: dict[str, float],
    output_dir: Path,
) -> None:
    items = (
        ("Peak temperature\nrise", "peak_temperature_rise_c"),
        ("Average leakage\npower", "chip_average_leakage_w"),
        ("Leakage energy", "leakage_energy_mj"),
        ("Total energy", "total_energy_mj"),
    )
    increases = [
        percent_delta(constant_metrics[key], aware_metrics[key])
        for _, key in items
    ]
    fig, axis = plt.subplots(figsize=(7.2, 3.6))
    x = np.arange(len(items))
    bars = axis.bar(x, increases, width=0.62, color=TEMPERATURE_COLOR, alpha=0.9)
    axis.axhline(0.0, color=CONSTANT_COLOR, linewidth=1.8,
                 label="Constant-reference baseline")
    axis.set_xticks(x, [label for label, _ in items])
    axis.set_ylabel("Change from constant-reference control (%)")
    axis.set_title("Impact of enabling live leakage-temperature feedback")
    axis.set_ylim(0.0, max(increases) * 1.22)
    axis.legend(frameon=False, loc="upper left")
    for bar, increase in zip(bars, increases):
        axis.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height(),
            f"{increase:+.1f}%",
            ha="center",
            va="bottom",
            fontweight="bold",
        )
    fig.tight_layout()
    save_figure(fig, output_dir, "figure_normalized_effect")


def write_metrics_csv(
    path: Path,
    constant_metrics: dict[str, float],
    aware_metrics: dict[str, float],
) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("metric", "unit", "constant", "temperature_aware", "absolute_delta", "relative_delta_pct"))
        for key in constant_metrics:
            constant = constant_metrics[key]
            aware = aware_metrics[key]
            relative = 100.0 * (aware - constant) / constant if constant else float("nan")
            writer.writerow((key, UNITS[key], constant, aware, aware - constant, relative))


def write_timeseries_csv(path: Path, runs: Iterable[RunData]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(
            (
                "profile",
                "phase",
                "start_ps",
                "end_ps",
                "component_path",
                "dynamic_w",
                "leakage_w",
                "total_w",
                "temperature_used_c",
                "temperature_returned_c",
            )
        )
        for run in runs:
            returned = {
                (row["phase"], row["start_ps"], row["end_ps"], row["path"]): row["temperature_c"]
                for row in run.temperatures
            }
            for record in run.power_records:
                window = record["window"]
                for component in record["components"]:
                    key = (
                        record["phase"],
                        window["start_ps"],
                        window["end_ps"],
                        component["path"],
                    )
                    writer.writerow(
                        (
                            run.profile,
                            record["phase"],
                            window["start_ps"],
                            window["end_ps"],
                            component["path"],
                            component["dynamic_w"],
                            component["leakage_w"],
                            component["total_w"],
                            component["temperature_c"],
                            returned[key],
                        )
                    )


def percent_delta(constant: float, aware: float) -> float:
    return 100.0 * (aware - constant) / constant


def metric_row(name: str, unit: str, constant: float, aware: float) -> str:
    return (
        f"| {name} | {constant:.4g} | {aware:.4g} | "
        f"{aware - constant:+.4g} {unit} | {percent_delta(constant, aware):+.2f}% |"
    )


def write_report(
    path: Path,
    constant: RunData,
    aware: RunData,
    constant_metrics: dict[str, float],
    aware_metrics: dict[str, float],
) -> None:
    peak_delta = aware_metrics["peak_temperature_c"] - constant_metrics["peak_temperature_c"]
    leakage_energy_delta = percent_delta(
        constant_metrics["leakage_energy_mj"], aware_metrics["leakage_energy_mj"]
    )
    total_energy_delta = percent_delta(
        constant_metrics["total_energy_mj"], aware_metrics["total_energy_mj"]
    )
    leakage_power_delta = percent_delta(
        constant_metrics["chip_average_leakage_w"],
        aware_metrics["chip_average_leakage_w"],
    )
    peak_rise_delta = percent_delta(
        constant_metrics["peak_temperature_rise_c"],
        aware_metrics["peak_temperature_rise_c"],
    )
    redmule_peak_leakage_delta = percent_delta(
        constant_metrics["active_redmule_peak_leakage_w"],
        aware_metrics["active_redmule_peak_leakage_w"],
    )
    tcdm_peak_leakage_delta = percent_delta(
        constant_metrics["active_tcdm_peak_leakage_w"],
        aware_metrics["active_tcdm_peak_leakage_w"],
    )
    node = aware.metadata.get("technology_node", "unknown")
    cluster_count = aware.metadata.get("cluster_grid", {}).get("count", "unknown")
    interval_ps = aware.summary.get("power_interval_ps", "unknown")
    simulator_config = portable_path(aware.summary.get("simulator_config", "unknown"))
    constant_run = portable_path(constant.path)
    aware_run = portable_path(aware.path)

    report = f"""# Temperature-dependent leakage in SoftHier–3D-ICE

## Abstract

This controlled experiment quantifies the effect of temperature-dependent
leakage in the runtime-coupled SoftHier–3D-ICE loop. Both simulations execute
the same default 4096³ GEMM on the same {node}, {cluster_count}-cluster design
with {int(float(interval_ps)) / 1.0e6:g} µs thermal exchanges. The control uses
the new 25 °C reference leakage for LightRedMulE and TCDM but holds it constant;
the treatment uses identical reference values and updates leakage from the live
3D-ICE temperatures. Temperature-aware leakage raises peak temperature by
**{peak_delta:.3f} °C**, average leakage power by **{leakage_power_delta:.2f}%**,
leakage energy by **{leakage_energy_delta:.2f}%**, and total modeled energy by
**{total_energy_delta:.2f}%**. These results demonstrate a positive
power–temperature feedback effect without conflating it with a different
nominal leakage assumption.

## Experimental design

| Item | Value |
|---|---|
| Workload | Provider-default 4096 × 4096 × 4096 GEMM, 4096 RedMulE tiles |
| Architecture | `{simulator_config}` |
| Technology | {node} |
| Thermally coupled components | 16 LightRedMulE + 16 TCDM = 32 |
| Power/thermal interval | {interval_ps} ps |
| Simulated application time | {aware_metrics['simulated_time_ms']:.6f} ms |
| Control profile | `constant` |
| Treatment profile | `temperature_aware` |
| Constant run | `{constant_run}` |
| Temperature-aware run | `{aware_run}` |

All architecture, workload, voltage lookup behavior, dynamic-energy tables,
thermal geometry, solver step, and initial conditions are held fixed. The sole
A/B factor is whether leakage remains at its 25 °C reference or follows the
temperature table.

## Leakage model

For each voltage point, the temperature-aware profile samples

```text
P_leak(T) = P_leak(25 °C) × 2^((T − 25 °C) / T_double)
```

at 25, 40, 55, 70, 85, 100, 115, and 125 °C. GVSoC linearly interpolates
between samples and clamps outside that characterized interval. At 5 nm, the
reference values are 3.4 µW/kGE for LightRedMulE and 20 mW/MiB for SRAM at
0.7 V; doubling temperatures are 22 °C and 27 °C, respectively. RedMulE power
scales with estimated gate count and SRAM power with instantiated capacity.
Dynamic energy retains the pre-existing tables. The platform's default 1.2 V
source query lies above the 5 nm table range and therefore clamps to the 0.7 V
endpoint in both runs; voltage behavior is identical across the A/B pair.

These values are physically plausible engineering assumptions chosen for a
sensitivity study, not measured silicon or sign-off characterization.

![Technology-scaled model assumptions](figure_model_scaling.png)

## Results

| Metric | Constant | Temperature-aware | Absolute change | Relative change |
|---|---:|---:|---:|---:|
{metric_row('Peak component temperature (°C)', '°C', constant_metrics['peak_temperature_c'], aware_metrics['peak_temperature_c'])}
{metric_row('Peak temperature rise above 26.85 °C (°C)', '°C', constant_metrics['peak_temperature_rise_c'], aware_metrics['peak_temperature_rise_c'])}
{metric_row('Average aggregate leakage power (W)', 'W', constant_metrics['chip_average_leakage_w'], aware_metrics['chip_average_leakage_w'])}
{metric_row('Peak aggregate leakage power (W)', 'W', constant_metrics['chip_peak_leakage_w'], aware_metrics['chip_peak_leakage_w'])}
{metric_row('Active LightRedMulE peak leakage (W)', 'W', constant_metrics['active_redmule_peak_leakage_w'], aware_metrics['active_redmule_peak_leakage_w'])}
{metric_row('Active TCDM peak leakage (W)', 'W', constant_metrics['active_tcdm_peak_leakage_w'], aware_metrics['active_tcdm_peak_leakage_w'])}
{metric_row('Leakage energy (mJ)', 'mJ', constant_metrics['leakage_energy_mj'], aware_metrics['leakage_energy_mj'])}
{metric_row('Total modeled energy (mJ)', 'mJ', constant_metrics['total_energy_mj'], aware_metrics['total_energy_mj'])}
{metric_row('Leakage energy fraction (%)', 'percentage points', constant_metrics['leakage_energy_fraction_pct'], aware_metrics['leakage_energy_fraction_pct'])}

The temperature-aware model increases the peak temperature rise by
**{peak_rise_delta:.2f}%** relative to the controlled constant-leakage case.
Dynamic energy changes by
**{percent_delta(constant_metrics['dynamic_energy_mj'], aware_metrics['dynamic_energy_mj']):.3f}%**;
the analyzer rejects the A/B pair if dynamic energy differs by more than one
part per million, protecting the controlled-work equivalence.

## Figures

![Closed-loop temperature comparison](figure_temperature_comparison.png)

![Power and energy comparison](figure_power_energy_comparison.png)

![Runtime leakage characteristic](figure_leakage_characteristic.png)

![Temperature-aware temperature and leakage over time](figure_temperature_leakage_trajectory.png)

The dual-axis time-series plot places simulated time on the x-axis, component
temperature on the left y-axis, and leakage power on the right y-axis. It makes
their concurrent rise directly visible for the active LightRedMulE and TCDM
components under the temperature-aware profile.

![Normalized feedback effect](figure_normalized_effect.png)

Vector PDF versions are stored beside every PNG for direct use in slides or
publication figures.

## Interpretation

The constant profile already includes realistic nominal standby power, but it
cannot amplify as the die heats. In the treatment, every 3D-ICE response is
fed back into GVSoC and immediately re-interpolates the leakage tables. The
result is a causal loop: higher temperature increases leakage, increased
leakage raises the next interval's total power, and that additional power raises
subsequent temperatures. The effect is visible both in the leakage-versus-time
curve and in the separation of the temperature trajectories.

## Slide-ready takeaways

- Live temperature-dependent leakage increases peak temperature by
  **{peak_delta:.3f} °C** ({peak_rise_delta:+.2f}% of temperature rise).
- At their peak runtime temperatures, active LightRedMulE and TCDM leakage are
  **{redmule_peak_leakage_delta:.2f}%** and **{tcdm_peak_leakage_delta:.2f}%**
  above their constant-reference values, respectively.
- Temperature-aware leakage energy is **{leakage_energy_delta:.2f}%** higher
  than the constant-reference estimate for this workload and assumed model.
- Total modeled energy is correspondingly **{total_energy_delta:.2f}%** higher,
  even though the dynamic-energy model and simulated execution are unchanged.

## Limitations

1. Leakage coefficients are synthetic, technology-scaled estimates, not a
   calibrated PDK/library characterization.
2. The model assumes instantiated RedMulE and TCDM blocks remain powered; it
   does not infer power gating for idle clusters.
3. The energy totals cover the 32 explicitly coupled RedMulE/TCDM components,
   not unmodeled cores, NoC, control logic, HBM, or package power.
4. This is one default GEMM workload and one thermal stack; conclusions should
   be reported as a sensitivity case rather than universal silicon prediction.
5. Voltage is not part of the feedback loop in this experiment; the same
   endpoint-clamped voltage lookup is used throughout both simulations.

## Reproducibility

```bash
make co-simulation RUN_NAME=leakage_constant \\
  SOFTHIER_POWER_PROFILE=constant SIMULATOR_LOG_TAIL_LINES=0

make co-simulation RUN_NAME=leakage_temperature_aware \\
  SOFTHIER_POWER_PROFILE=temperature_aware SIMULATOR_LOG_TAIL_LINES=0

MPLCONFIGDIR=/tmp/softhier-leakage-mpl \\
  conda run -n py312 python experiments/leakage_temperature/analyze.py \\
  --constant-run runs/leakage_constant/latest \\
  --temperature-aware-run runs/leakage_temperature_aware/latest \\
  --output-dir experiments/leakage_temperature/results/<study-id>
```

Machine-readable results are in `metrics.csv`, `comparison.json`, and
`timeseries.csv`. The complete generated lookup points for every supported
technology node are in `leakage_model_tables.csv`.
"""
    path.write_text(report, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--constant-run", type=Path, required=True)
    parser.add_argument("--temperature-aware-run", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    constant = load_run(args.constant_run)
    aware = load_run(args.temperature_aware_run)
    if constant.profile != "constant":
        raise ValueError(f"expected constant profile, got {constant.profile!r}")
    if aware.profile != "temperature_aware":
        raise ValueError(f"expected temperature_aware profile, got {aware.profile!r}")

    for field in ("technology_node", "cluster_grid", "core_count_per_cluster"):
        if constant.metadata.get(field) != aware.metadata.get(field):
            raise ValueError(f"A/B runs differ in {field}")
    for field in (
        "simulator_config",
        "simulator_app",
        "power_interval_ps",
        "3dice_mode",
        "3dice_slot_seconds",
        "3dice_step_seconds",
    ):
        if constant.summary.get(field) != aware.summary.get(field):
            raise ValueError(f"A/B runs differ in {field}")
    if constant.duration_ps != aware.duration_ps:
        raise ValueError("A/B runs have different simulated durations")
    constant_paths = tuple(
        component["path"] for component in constant.power_records[0]["components"]
    )
    aware_paths = tuple(
        component["path"] for component in aware.power_records[0]["components"]
    )
    if constant_paths != aware_paths:
        raise ValueError("A/B runs have different coupled component sets")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    constant_metrics = metrics(constant)
    aware_metrics = metrics(aware)
    if not math.isclose(
        constant_metrics["dynamic_energy_mj"],
        aware_metrics["dynamic_energy_mj"],
        rel_tol=1.0e-6,
        abs_tol=1.0e-9,
    ):
        raise ValueError("A/B runs do not have matching dynamic-energy work")

    configure_style()
    plot_temperature(constant, aware, output_dir)
    plot_power_energy(constant, aware, constant_metrics, aware_metrics, output_dir)
    plot_characteristic(constant, aware, output_dir)
    plot_temperature_leakage_trajectory(aware, output_dir)
    plot_normalized_effect(constant_metrics, aware_metrics, output_dir)
    plot_model_scaling(output_dir)
    write_metrics_csv(output_dir / "metrics.csv", constant_metrics, aware_metrics)
    write_timeseries_csv(output_dir / "timeseries.csv", (constant, aware))
    write_model_tables(output_dir / "leakage_model_tables.csv")

    comparison = {
        "method": "controlled constant-reference versus temperature-aware leakage",
        "constant_run": portable_path(constant.path),
        "temperature_aware_run": portable_path(aware.path),
        "constant": constant_metrics,
        "temperature_aware": aware_metrics,
        "delta": {
            key: aware_metrics[key] - constant_metrics[key] for key in constant_metrics
        },
        "relative_delta_pct": {
            key: percent_delta(constant_metrics[key], aware_metrics[key])
            if constant_metrics[key]
            else None
            for key in constant_metrics
        },
    }
    (output_dir / "comparison.json").write_text(
        json.dumps(comparison, indent=2) + "\n", encoding="utf-8"
    )
    write_report(
        output_dir / "REPORT.md",
        constant,
        aware,
        constant_metrics,
        aware_metrics,
    )
    print(f"Wrote leakage-temperature study to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
