#!/usr/bin/env python3
"""Acceptance checks for a completed synthetic LLM simulation."""
import argparse
import csv
import json
import math
from pathlib import Path
import re


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_run(run, coupled=True, regression=False):
    run = Path(run)
    log = (run / "logs/simulator.log").read_text(errors="replace")
    marker = "Kernel regression: PASS" if regression else "Finite hidden output: PASS"
    require("[LLMForward] Application exit: 0" in log, "missing successful application exit marker")
    require(marker in log, f"missing application completion marker: {marker}")
    require("[LLMForward] ERROR" not in log, "application reported an error")
    result = {"application": "passed", "numerics": "kernel regression" if regression else "all final hidden values finite"}
    if not coupled:
        return result
    summary = (run / "summary.txt").read_text()
    require(re.search(r"^simulator: 0$", summary, re.M), "simulator did not exit successfully")
    require(re.search(r"^3dice_server: 0$", summary, re.M), "3D-ICE server did not exit successfully")
    contract = json.loads((run / "generated/system_config.json").read_text())
    components = contract["thermal_feedback"]["components"]
    expected = {item["path"] for item in components}
    require(len(expected) == len(components), "duplicate component inventory")
    phases, previous_end, exchanges = [], 0, 0
    with (run / "traces/power_hook_trace.jsonl").open() as stream:
        for line in stream:
            record = json.loads(line)
            phase = record["phase"]
            require(phase in ("init", "update", "final"), "unknown hook phase")
            phases.append(phase)
            if phase == "init":
                require(exchanges == 0 and not record["components"], "invalid init record")
            else:
                items = record["components"]
                require(len(items) == len(expected) and {item["path"] for item in items} == expected,
                        "power trace must cover every component exactly once")
                for item in items:
                    require(all(math.isfinite(item[k]) and item[k] >= 0 for k in ("dynamic_w", "leakage_w", "total_w")), "invalid power")
                    require(math.isfinite(item["temperature_c"]) and item["temperature_c"] > -273.15, "invalid feedback temperature")
                window = record["window"]
                require(window["start_ps"] == previous_end and window["end_ps"] >= previous_end, "non-contiguous power windows")
                previous_end = window["end_ps"]
            exchanges += 1
    require(phases and phases[0] == "init" and phases[-1] == "final" and phases.count("init") == phases.count("final") == 1 and "update" in phases,
            "expected init, update, and final hook phases")
    widths = len(contract["floorplan"]["elements"])
    rows = (run / "traces/3dice_power_traces.txt").read_text().splitlines()
    power = [[float(x) for x in row.split()] for row in rows if row.strip() and not row.startswith("%")]
    require(power and len(power[-1]) == widths and all(v == -1 for v in power[-1]), "missing final power sentinel")
    for row in power[:-1]:
        require(len(row) == widths and all(math.isfinite(v) and v >= 0 for v in row), "invalid thermal power row")
    with (run / "traces/component_temperatures.csv").open() as stream:
        temps = list(csv.DictReader(stream))
    require(temps and {r["component_path"] for r in temps} == expected, "missing component temperatures")
    require(all(math.isfinite(float(r["temperature_c"])) and float(r["temperature_c"]) > -273.15 for r in temps), "non-finite solved temperature")
    result.update(hook_exchanges=exchanges, component_count=len(expected), duration_ps=previous_end,
                  max_temperature_c=max(float(r["temperature_c"]) for r in temps), thermal="passed")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--standalone", action="store_true")
    parser.add_argument("--regression", action="store_true")
    args = parser.parse_args()
    print(json.dumps(validate_run(args.run, not args.standalone, args.regression), indent=2))
