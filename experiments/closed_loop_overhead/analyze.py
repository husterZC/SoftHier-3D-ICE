"""Exclusive host timing, randomized-block statistics, and paper figures."""
from collections import Counter, defaultdict
import bisect
import csv
import json
from pathlib import Path
import random
import statistics

PHASES = ("initialization", "architecture", "power_capture", "thermal_advancement",
          "coupling_output", "completion")


def coupling_diagnostics(events, roi):
    """Nested diagnostics, never added to the exclusive wall-time partition."""
    steps = sorted((e for e in events if e["phase"] == "thermal_step"), key=lambda e: e["start_ns"])
    starts = [e["start_ns"] for e in steps]
    ready = [e for e in events if e["phase"] == "power_mapping"
             and roi[0] <= e["start_ns"] and e["end_ns"] <= roi[1]]
    latencies = []
    for event in ready:
        i = bisect.bisect_left(starts, event["end_ns"])
        if i and steps[i - 1]["end_ns"] > event["end_ns"]:
            i -= 1
        if i < len(steps) and steps[i]["end_ns"] <= roi[1]:
            latencies.append(max(0, steps[i]["start_ns"] - event["end_ns"]) * 1e-9)
    phases = ("configuration", "request_validation", "power_mapping", "temperature_wait",
              "feedback_mapping", "response_write", "worker_roundtrip")
    return dict(power_to_solver_count=len(latencies), power_to_solver_seconds=sum(latencies),
                mean_power_to_solver_seconds=statistics.mean(latencies) if latencies else 0,
                p95_power_to_solver_seconds=percentile(latencies, .95) if latencies else 0,
                nested_roi_seconds={phase: sum(max(0, min(roi[1], e["end_ns"]) - max(roi[0], e["start_ns"])) * 1e-9
                    for e in events if e["phase"] == phase) for phase in phases})


def read_events(directory):
    events = []
    for path in sorted(Path(directory).glob("*.jsonl")):
        for line in path.read_text().splitlines():
            event = json.loads(line)
            if event["end_ns"] < event["start_ns"]:
                raise ValueError(f"negative timing span: {path}")
            events.append(event)
    return events


def partition(events, start, end, roi):
    """Sweep nested/overlapping spans once; blocked waits contain solver work.

    Initialization/completion are outside ROI, regardless of their internal work.
    Inside ROI, native thermal work takes priority over capture and hook waits.
    Remaining time is architecture execution, including inline power accounting.
    """
    if end <= start:
        raise ValueError("empty measurement interval")
    spans = [(start, min(end, roi[0]), 5, "initialization"),
             (max(start, roi[1]), end, 5, "completion")]
    for e in events:
        phase = e["phase"]
        if phase == "exchange":
            priority, category = 1, "coupling_output"
        elif phase == "power_capture":
            priority, category = 2, "power_capture"
        elif phase == "thermal_output":
            priority, category = 3, "coupling_output"
        elif phase == "thermal_step":
            priority, category = 4, "thermal_advancement"
        else:
            continue
        spans.append((max(start, e["start_ns"]), min(end, e["end_ns"]), priority, category))
    boundaries = defaultdict(list)
    for left, right, priority, category in spans:
        if right > left:
            boundaries[left].append(((priority, category), 1))
            boundaries[right].append(((priority, category), -1))
    boundaries[start]; boundaries[end]
    active = Counter({(0, "architecture"): 1})
    result = dict.fromkeys(PHASES, 0)
    previous = start
    for timestamp in sorted(boundaries):
        category = max(key for key, count in active.items() if count > 0)[1]
        result[category] += timestamp - previous
        for key, delta in boundaries[timestamp]:
            active[key] += delta
        previous = timestamp
    if sum(result.values()) != end - start:
        raise ValueError("timing partition does not reconcile")
    return {key: value * 1e-9 for key, value in result.items()}


def summarize_events(events, start, end, thermal, interval_ps):
    starts = [e for e in events if e["phase"] == "roi_start"]
    ends = [e for e in events if e["phase"] == "roi_end"]
    if len(starts) != 1 or len(ends) != 1:
        raise ValueError("expected one native ROI start/end pair; rebuild instrumented hardware")
    roi = (starts[0]["start_ns"], ends[0]["end_ns"])
    if not start <= roi[0] < roi[1] <= end:
        raise ValueError("ROI markers lie outside the measured execution")
    durations = defaultdict(list)
    processes = {}
    for e in events:
        durations[e["phase"]].append((e["end_ns"] - e["start_ns"]) * 1e-9)
        process = processes.setdefault(str(e["pid"]), dict(actor=e["actor"], user_us=0,
                                                          system_us=0, maxrss_kb=0))
        for key in ("user_us", "system_us", "maxrss_kb"):
            process[key] = max(process[key], e.get(key, 0))
    exchanges = [e for e in events if e["phase"] == "exchange" and e["value"] > 0]
    steps = [e for e in events if e["phase"] == "thermal_step"]
    factor = [e for e in events if e["phase"] == "factorization"]
    cells = [e["value"] for e in events if e["phase"] == "mesh_cells"]
    nnz = [e["value"] for e in events if e["phase"] == "matrix_nnz"]
    if thermal and (not exchanges or len(steps) != len(exchanges) or len(factor) != 1
                    or len(cells) != 1 or len(nnz) != 1):
        raise ValueError("missing native thermal instrumentation or steps/exchanges/factorizations disagree")
    if not thermal and (exchanges or steps or cells):
        raise ValueError("architecture-only run performed power/thermal exchanges")
    if any(e["value"] > interval_ps for e in exchanges):
        raise ValueError("power exchange exceeded the configured interval")
    latency = [(e["end_ns"] - e["start_ns"]) * 1e-9 for e in exchanges]
    return dict(end_to_end_seconds=(end - start) * 1e-9,
                roi_seconds=(roi[1] - roi[0]) * 1e-9,
                roi_simulated_ps=ends[0]["sim_ps"] - starts[0]["sim_ps"],
                phases_end_to_end=partition(events, start, end, roi),
                phases_roi=partition(events, *roi, roi),
                detail_seconds={key: sum(values) for key, values in durations.items()},
                exchange_count=len(exchanges), thermal_steps=len(steps), factorizations=len(factor),
                native_cells=cells[0] if cells else 0, matrix_nnz=nnz[0] if nnz else 0,
                mean_exchange_seconds=statistics.mean(latency) if latency else 0,
                p95_exchange_seconds=percentile(latency, .95) if latency else 0,
                exchange_latency_seconds=latency,
                solver_seconds_per_step=(sum(durations["thermal_solve"]) / len(steps)) if steps else 0,
                processes=processes, coupling_diagnostics=coupling_diagnostics(events, roi))


def percentile(values, fraction):
    values = sorted(values)
    index = (len(values) - 1) * fraction
    lower = int(index)
    return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (index - lower)


def block_statistics(rows, configurations, samples=10000, seed=19):
    """Ratio of means, resampling whole paired blocks (not exchange samples)."""
    blocks = defaultdict(dict)
    for row in rows:
        if row["status"] == "complete" and not row["pilot"]:
            key = row["configuration"]
            if key in blocks[row["block"]]:
                raise ValueError("duplicate configuration in a measured block")
            blocks[row["block"]][key] = row
    complete = [b for _, b in sorted(blocks.items()) if set(b) == set(configurations)]
    if not complete:
        raise ValueError("no complete measured blocks; failed/partial runs cannot be analyzed as results")
    rng = random.Random(seed)
    indices = [[rng.randrange(len(complete)) for _ in complete] for _ in range(samples)]
    output = []
    for name in configurations:
        for scope in ("end_to_end_seconds", "roi_seconds"):
            values = [b[name]["timing"][scope] for b in complete]
            base = [b["architecture_only"]["timing"][scope] for b in complete]
            mean = statistics.mean(values)
            ratio = mean / statistics.mean(base)
            mean_samples = [statistics.mean(values[i] for i in ix) for ix in indices]
            ratio_samples = [sum(values[i] for i in ix) / sum(base[i] for i in ix) for ix in indices]
            ci = lambda data: [percentile(data, .025), percentile(data, .975)] if len(complete) > 1 else None
            output.append(dict(configuration=name, scope=scope, mean_seconds=mean, slowdown=ratio,
                               overhead_percent=100 * (ratio - 1), mean_ci95=ci(mean_samples),
                               slowdown_ci95=ci(ratio_samples), complete_blocks=len(complete)))
    return output, complete


def analyze(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / "study.json").read_text())
    rows = []
    for item in manifest["schedule"]:
        path = directory / "runs" / item["id"] / "measurement.json"
        if path.is_file():
            rows.append(json.loads(path.read_text()))
    stats, blocks = block_statistics(rows, manifest["configurations"], seed=manifest["settings"]["seed"])
    results = dict(schema_version=1, statistics=stats,
                   coupling_mode=manifest["settings"].get("coupling_mode", "legacy"),
                   requested_blocks=manifest["settings"]["replicates"], complete_blocks=len(blocks),
                   excluded_runs=[r["id"] for r in rows if r["status"] != "complete"],
                   missing_runs=[i["id"] for i in manifest["schedule"] if i["id"] not in {r["id"] for r in rows}],
                   claim="B8 total closed-loop cost; no grid-accuracy claim")
    (directory / "analysis.json").write_text(json.dumps(results, indent=2) + "\n")
    with (directory / "summary.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(stats[0]))
        writer.writeheader(); writer.writerows(stats)
    flat = []
    for row in rows:
        record = {key: row[key] for key in ("id", "configuration", "block", "pilot", "status")}
        record["coupling_mode"] = manifest["settings"].get("coupling_mode", "legacy")
        timing = row.get("timing", {})
        record.update({key: timing.get(key) for key in ("end_to_end_seconds", "roi_seconds", "native_cells",
                       "matrix_nnz", "exchange_count", "thermal_steps", "factorizations", "solver_seconds_per_step")})
        flat.append(record)
    with (directory / "runs.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(flat[0])); writer.writeheader(); writer.writerows(flat)
    figures(directory, stats, blocks, manifest["configurations"])
    return results


def figures(directory, stats, blocks, names):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    labels = ["Architecture" if n == "architecture_only" else n.replace("closed_loop_", "") + " µm" for n in names]
    def save(fig, name):
        fig.tight_layout()
        for suffix in ("pdf", "png"):
            fig.savefig(directory / f"{name}.{suffix}", dpi=200)
        plt.close(fig)
    fig, axes = plt.subplots(2, 2, figsize=(10, 7))
    for i, scope in enumerate(("end_to_end_seconds", "roi_seconds")):
        data = [next(s for s in stats if s["configuration"] == n and s["scope"] == scope) for n in names]
        for j, (key, ci) in enumerate((("mean_seconds", "mean_ci95"), ("slowdown", "slowdown_ci95"))):
            values = [s[key] for s in data]
            errors = np.array([[max(0, s[key] - s[ci][0]), max(0, s[ci][1] - s[key])] if s[ci] else [0, 0] for s in data]).T
            axes[i, j].bar(labels, values, yerr=errors, capsize=4)
            axes[i, j].set_ylabel("Host seconds" if j == 0 else "Slowdown (×)")
            axes[i, j].set_title("End to end" if i == 0 else "Workload ROI")
    save(fig, "runtime")
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    for ax, scope in zip(axes, ("phases_end_to_end", "phases_roi")):
        bottom = np.zeros(len(names))
        for phase in PHASES:
            values = np.array([statistics.mean(b[n]["timing"][scope][phase] for b in blocks) for n in names])
            ax.bar(labels, values, bottom=bottom, label=phase.replace("_", " "))
            bottom += values
        ax.set_ylabel("Host seconds"); ax.set_title(scope.replace("phases_", "").replace("_", " "))
    axes[1].legend(fontsize=8)
    save(fig, "phases")
    fig, axes = plt.subplots(1, 3, figsize=(12, 3.6))
    thermal = [n for n in names if n != "architecture_only"]
    cells = [blocks[0][n]["timing"]["native_cells"] for n in thermal]
    metrics = ("end_to_end_seconds", "solver_seconds_per_step", "ice_peak_mib")
    for ax, key, label in zip(axes, metrics, ("Host seconds", "Solve seconds / step", "3D-ICE peak RSS (MiB)")):
        def value(b, n):
            t = b[n]["timing"]
            if key == "ice_peak_mib":
                return max((p["maxrss_kb"] / 1024 for p in t["processes"].values() if p["actor"] == "ice"), default=0)
            return t[key]
        values = [statistics.mean(value(b, n) for b in blocks) for n in thermal]
        ax.plot(cells, values, "o-"); ax.set_xlabel("Native thermal cells"); ax.set_ylabel(label)
    save(fig, "mesh_cost")
