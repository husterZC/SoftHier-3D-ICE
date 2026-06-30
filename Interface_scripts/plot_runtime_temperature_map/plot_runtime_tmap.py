#
# Copyright (C) 2025 ETH Zurich and University of Bologna
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

# Author:    Kai Zhu   <kai.zhu@epfl.ch>

"""Runtime temperature-map viewer for 3D-ICE outputs.

GUI Tmap mode:
  python3 plot_runtime_tmap.py --coords COORDS_FILE --map TMAP_FILE

Headless floorplan dashboard mode:
  python3 plot_runtime_tmap.py --floorplan FLOORPLAN_FILE --tflp TFLP_FILE --html HTML_FILE

Examples:
  python3 plot_runtime_tmap.py --coords output_top_die_map.coords.txt --map output_top_die_map.txt
  python3 plot_runtime_tmap.py \
      --floorplan runs/default/latest/results/3dice/floorplan_nopower.flp \
      --tflp runs/default/latest/results/3dice/output_top_die_flp_avg.txt \
      --html runs/default/latest/results/3dice/temperature_map.html --once

Notes:
  - GUI Tmap mode opens a Matplotlib window and needs a display-capable backend.
  - HTML floorplan mode is designed for SSH/headless runs and does not need Matplotlib.
  - Follow mode is the default. In HTML mode, follow mode rewrites the HTML file when
    complete Tflp rows are appended.
"""

import argparse
import html
import json
import re
import sys
import time
from pathlib import Path


FLOORPLAN_ENTRY_RE = re.compile(
    r"^\s*([^:\n]+?)\s*:\s*(?:\r?\n)(.*?)(?=^\s*[^:\n]+?\s*:\s*(?:\r?\n)|\Z)",
    re.MULTILINE | re.DOTALL,
)
POSITION_RE = re.compile(r"\bposition\s+([^,;]+)\s*,\s*([^;]+)\s*;", re.IGNORECASE)
DIMENSION_RE = re.compile(r"\bdimension\s+([^,;]+)\s*,\s*([^;]+)\s*;", re.IGNORECASE)
TFLP_NAME_RE = re.compile(r"([^\s\t()]+)\(K\)")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Render runtime 3D-ICE temperature maps as a GUI plot or headless HTML dashboard."
    )
    parser.add_argument(
        "--coords",
        help="Path to a Tmap coordinate file, for example output_top_die_map.coords.txt.",
    )
    parser.add_argument(
        "--map",
        help="Path to a Tmap temperature file, for example output_top_die_map.txt.",
    )
    parser.add_argument(
        "--floorplan",
        help="Path to a 3D-ICE floorplan file, for example floorplan_nopower.flp.",
    )
    parser.add_argument(
        "--tflp",
        help="Path to a 3D-ICE Tflp output file, for example output_top_die_flp_avg.txt.",
    )
    parser.add_argument(
        "--html",
        help="Path to write a self-contained HTML dashboard for --floorplan/--tflp mode.",
    )
    parser.add_argument("--poll", type=float, default=1.0)
    parser.add_argument("--html-refresh", type=float)
    parser.add_argument("--cmap", default="inferno")
    parser.add_argument("--vmin", type=float)
    parser.add_argument("--vmax", type=float)
    parser.add_argument("--backend", default="QtAgg")
    parser.add_argument("--skip-to-latest", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--quiet", action="store_true")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--follow", action="store_true")
    mode.add_argument("--once", action="store_true")

    return parser.parse_args()


def validate_mode_args(args):
    html_fields = [args.floorplan, args.tflp, args.html]
    tmap_fields = [args.coords, args.map]
    html_mode = any(value is not None for value in html_fields)
    tmap_mode = any(value is not None for value in tmap_fields)

    if html_mode and tmap_mode:
        raise ValueError("choose either --floorplan/--tflp/--html mode or --coords/--map mode, not both")

    if html_mode:
        missing = [name for name, value in (("--floorplan", args.floorplan), ("--tflp", args.tflp), ("--html", args.html)) if value is None]
        if missing:
            raise ValueError(f"HTML mode requires {', '.join(missing)}")
        return "html"

    if tmap_mode:
        missing = [name for name, value in (("--coords", args.coords), ("--map", args.map)) if value is None]
        if missing:
            raise ValueError(f"Tmap GUI mode requires {', '.join(missing)}")
        return "tmap"

    raise ValueError("provide either --floorplan/--tflp/--html or --coords/--map")


def load_plotting_modules(backend):
    try:
        import numpy as np
        import matplotlib

        matplotlib.use(backend)

        import matplotlib.pyplot as plt
        from matplotlib.collections import PolyCollection
    except ImportError as exc:
        print(
            "ERROR: missing Python plotting dependency.\n"
            "Install GUI plotting dependencies with:\n"
            "  python3 -m pip install -r requirements_tmap_plot.txt\n"
            "For SSH/headless runs, use --floorplan/--tflp/--html instead.\n"
            f"Original import error: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    except Exception as exc:
        print(
            f"ERROR: could not initialize matplotlib backend {backend!r}: {exc}\n"
            "For a local GUI window, install/use a GUI backend such as PyQt5 and run with X11/desktop access.\n"
            "For SSH/headless runs, use --floorplan/--tflp/--html instead.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    return np, plt, PolyCollection


def parse_nrows_ncolumns(line):
    parts = line.strip().split()

    if len(parts) >= 5 and parts[0] == "#" and parts[1] == "nrows" and parts[3] == "ncolumns":
        return int(parts[2]), int(parts[4])

    return None, None


def load_geometry(coords_path, np):
    cells = []
    declared_nrows = None
    declared_ncolumns = None

    with coords_path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            stripped = line.strip()

            if not stripped:
                continue

            if stripped.startswith("#"):
                nrows, ncolumns = parse_nrows_ncolumns(stripped)

                if nrows is not None:
                    declared_nrows = nrows
                    declared_ncolumns = ncolumns

                continue

            parts = stripped.split()

            if len(parts) != 4:
                raise ValueError(
                    f"{coords_path}:{line_number}: expected 4 coordinate fields, got {len(parts)}"
                )

            cells.append(tuple(map(float, parts)))

    if not cells:
        raise ValueError(f"{coords_path}: no coordinate cells found")

    cells_array = np.asarray(cells, dtype=np.float64)
    left_x = cells_array[:, 0]
    left_y = cells_array[:, 1]
    length = cells_array[:, 2]
    width = cells_array[:, 3]
    right_x = left_x + length
    right_y = left_y + width

    vertices = np.empty((len(cells_array), 4, 2), dtype=np.float64)
    vertices[:, 0, 0] = left_x
    vertices[:, 0, 1] = left_y
    vertices[:, 1, 0] = right_x
    vertices[:, 1, 1] = left_y
    vertices[:, 2, 0] = right_x
    vertices[:, 2, 1] = right_y
    vertices[:, 3, 0] = left_x
    vertices[:, 3, 1] = right_y

    bounds = (
        float(left_x.min()),
        float(left_y.min()),
        float(right_x.max()),
        float(right_y.max()),
    )

    return {
        "vertices": vertices,
        "cell_count": len(cells_array),
        "bounds": bounds,
        "declared_nrows": declared_nrows,
        "declared_ncolumns": declared_ncolumns,
    }


def create_figure(args, geometry, np, plt, PolyCollection):
    min_x, min_y, max_x, max_y = geometry["bounds"]
    physical_width = max_x - min_x
    physical_height = max_y - min_y

    figure_width = 10.0
    figure_height = max(1.0, figure_width * physical_height / physical_width)

    figure, axis = plt.subplots(figsize=(figure_width, figure_height), constrained_layout=True)
    axis.set_aspect("equal", adjustable="box")
    axis.set_xlim(min_x, max_x)
    axis.set_ylim(min_y, max_y)
    axis.set_xlabel("x")
    axis.set_ylabel("y")

    initial_temperatures = np.zeros(geometry["cell_count"], dtype=np.float64)
    collection = PolyCollection(
        geometry["vertices"],
        array=initial_temperatures,
        cmap=args.cmap,
        edgecolors="none",
        linewidths=0,
        antialiased=False,
        rasterized=True,
    )
    axis.add_collection(collection)

    colorbar = figure.colorbar(collection, ax=axis)
    colorbar.set_label("Temperature (K)")
    axis.set_title("Waiting for Tmap data")

    return figure, axis, collection, colorbar


def parse_temperature_row(raw_line, expected_count, map_path, line_number, np):
    temperatures = np.fromstring(raw_line, sep=" ", dtype=np.float64)

    if len(temperatures) != expected_count:
        raise ValueError(
            f"{map_path}:{line_number}: expected {expected_count} temperatures, got {len(temperatures)}"
        )

    return temperatures


def first_non_whitespace_byte(raw_line):
    for byte in raw_line:
        if byte not in b" \t\n\r\v\f":
            return byte

    return None


def read_complete_data_line(stream, line_number, follow):
    while True:
        offset = stream.tell()
        raw_line = stream.readline()

        if not raw_line:
            stream.seek(offset)
            return None, line_number

        if follow and not raw_line.endswith(b"\n"):
            stream.seek(offset)
            return None, line_number

        line_number += 1
        first_byte = first_non_whitespace_byte(raw_line)

        if first_byte is None or first_byte == ord("#"):
            continue

        return raw_line, line_number


def read_complete_row(stream, map_path, expected_count, line_number, follow, np):
    raw_line, line_number = read_complete_data_line(stream, line_number, follow)

    if raw_line is None:
        return None, line_number

    return parse_temperature_row(raw_line, expected_count, map_path, line_number, np), line_number


def read_latest_existing_row(map_path, expected_count, np):
    latest_raw_line = None
    latest_line_number = None
    row_count = 0
    line_number = 0

    with map_path.open("rb") as stream:
        while True:
            raw_line, line_number = read_complete_data_line(
                stream, line_number, follow=False
            )

            if raw_line is None:
                break

            latest_raw_line = raw_line
            latest_line_number = line_number
            row_count += 1

    if latest_raw_line is None:
        return None, -1

    return (
        parse_temperature_row(
            latest_raw_line, expected_count, map_path, latest_line_number, np
        ),
        row_count - 1,
    )


def update_plot(args, figure, axis, collection, colorbar, temperatures, slot_index):
    # By default, scale the colors to the min/max of the row being rendered.
    min_temp = float(temperatures.min())
    max_temp = float(temperatures.max())
    vmin = args.vmin if args.vmin is not None else min_temp
    vmax = args.vmax if args.vmax is not None else max_temp

    if vmax <= vmin:
        vmax = vmin + 1.0

    collection.set_array(temperatures)
    collection.set_clim(vmin, vmax)
    colorbar.update_normal(collection)
    axis.set_title(
        f"Slot {slot_index} | min {min_temp:.3f} K | max {max_temp:.3f} K | "
        f"updated {time.strftime('%H:%M:%S')}"
    )
    figure.canvas.draw_idle()
    figure.canvas.flush_events()

    if not args.quiet:
        print(
            f"Rendered slot {slot_index}: {min_temp:.3f} K to {max_temp:.3f} K",
            flush=True,
        )


def wait_for_file(path, args, plt, figure):
    printed = False

    while not path.exists():
        if not printed and not args.quiet:
            print(f"Waiting for {path} ...", flush=True)
            printed = True

        if not plt.fignum_exists(figure.number):
            raise SystemExit(0)

        plt.pause(args.poll)


def render_once(args, map_path, geometry, np, plt, figure, axis, collection, colorbar):
    temperatures, slot_index = read_latest_existing_row(
        map_path, geometry["cell_count"], np
    )

    if temperatures is None:
        raise ValueError(f"{map_path}: no complete temperature rows found")

    update_plot(args, figure, axis, collection, colorbar, temperatures, slot_index)

    if not args.quiet:
        print("Close the matplotlib window to exit.", flush=True)

    plt.show()


def render_follow(args, map_path, geometry, np, plt, figure, axis, collection, colorbar):
    slot_index = -1
    line_number = 0

    wait_for_file(map_path, args, plt, figure)

    with map_path.open("rb") as stream:
        while plt.fignum_exists(figure.number):
            latest_raw_line = None
            latest_line_number = None
            count = 0

            while True:
                raw_line, line_number = read_complete_data_line(
                    stream, line_number, follow=True
                )

                if raw_line is None:
                    break

                latest_raw_line = raw_line
                latest_line_number = line_number
                count += 1

            if latest_raw_line is not None:
                slot_index += count
                latest = parse_temperature_row(
                    latest_raw_line,
                    geometry["cell_count"],
                    map_path,
                    latest_line_number,
                    np,
                )
                update_plot(args, figure, axis, collection, colorbar, latest, slot_index)
                plt.pause(0.001)
                continue

            try:
                if map_path.stat().st_size < stream.tell():
                    stream.seek(0)
                    line_number = 0
                    slot_index = -1

                    if not args.quiet:
                        print("Map file was truncated; restarting from the beginning.", flush=True)
            except FileNotFoundError:
                stream.close()
                wait_for_file(map_path, args, plt, figure)
                stream = map_path.open("rb")
                line_number = 0
                slot_index = -1

            plt.pause(args.poll)


def load_floorplan_regions(floorplan_path):
    text = floorplan_path.read_text(encoding="utf-8")
    regions = []

    for match in FLOORPLAN_ENTRY_RE.finditer(text):
        name = match.group(1).strip()
        body = match.group(2)
        position = POSITION_RE.search(body)
        dimension = DIMENSION_RE.search(body)

        if position is None or dimension is None:
            continue

        x = float(position.group(1))
        y = float(position.group(2))
        width = float(dimension.group(1))
        height = float(dimension.group(2))

        if width <= 0.0 or height <= 0.0:
            raise ValueError(f"{floorplan_path}: region {name!r} has non-positive dimensions")

        regions.append({"name": name, "x": x, "y": y, "width": width, "height": height})

    if not regions:
        raise ValueError(f"{floorplan_path}: no floorplan regions with position/dimension found")

    return regions


def parse_tflp_header(line):
    if not line.lstrip().startswith("%") or "Time(s)" not in line:
        return None

    names = TFLP_NAME_RE.findall(line)
    return names if names else None


def load_tflp_rows(tflp_path, follow=False):
    names = None
    rows = []

    with tflp_path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if follow and not line.endswith("\n"):
                break

            stripped = line.strip()

            if not stripped:
                continue

            if stripped.startswith("%"):
                parsed_names = parse_tflp_header(stripped)
                if parsed_names is not None:
                    names = parsed_names
                continue

            if names is None:
                raise ValueError(f"{tflp_path}:{line_number}: data row found before Tflp header")

            parts = stripped.split()
            expected = len(names) + 1

            if len(parts) != expected:
                raise ValueError(f"{tflp_path}:{line_number}: expected {expected} values, got {len(parts)}")

            rows.append({"time": float(parts[0]), "values": [float(value) for value in parts[1:]]})

    return names or [], rows


def floorplan_bounds(regions):
    min_x = min(region["x"] for region in regions)
    min_y = min(region["y"] for region in regions)
    max_x = max(region["x"] + region["width"] for region in regions)
    max_y = max(region["y"] + region["height"] for region in regions)

    return {"minX": min_x, "minY": min_y, "maxX": max_x, "maxY": max_y, "width": max_x - min_x, "height": max_y - min_y}


def align_regions_to_tflp(floorplan_path, regions, names):
    if not names:
        return regions

    by_name = {region["name"]: region for region in regions}
    missing = [name for name in names if name not in by_name]

    if missing:
        shown = ", ".join(missing[:8])
        if len(missing) > 8:
            shown += f", ... ({len(missing)} total)"
        raise ValueError(f"{floorplan_path}: missing floorplan regions for Tflp columns: {shown}")

    return [by_name[name] for name in names]


def html_number(value):
    return f"{value:.6g}"


def write_text_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def build_html_dashboard(args, floorplan_path, tflp_path, html_path, regions, names, rows, refresh_seconds):
    bounds = floorplan_bounds(regions)
    values = [temperature for row in rows for temperature in row["values"]]

    if values:
        data_min = min(values)
        data_max = max(values)
    else:
        data_min = args.vmin if args.vmin is not None else 300.0
        data_max = args.vmax if args.vmax is not None else data_min + 1.0

    vmin = args.vmin if args.vmin is not None else data_min
    vmax = args.vmax if args.vmax is not None else data_max

    if vmax <= vmin:
        vmax = vmin + 1.0

    payload = {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "floorplanPath": str(floorplan_path),
        "tflpPath": str(tflp_path),
        "htmlPath": str(html_path),
        "regions": regions,
        "names": names if names else [region["name"] for region in regions],
        "rows": rows,
        "bounds": bounds,
        "vmin": vmin,
        "vmax": vmax,
        "dataMin": data_min,
        "dataMax": data_max,
        "cmap": args.cmap,
    }
    payload_json = json.dumps(payload, separators=(",", ":"))
    refresh_tag = ""

    if refresh_seconds and refresh_seconds > 0.0:
        refresh_tag = f'<meta http-equiv="refresh" content="{html.escape(html_number(refresh_seconds))}">'

    safe_title = "3D-ICE Runtime Temperature Map"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh_tag}
<title>{safe_title}</title>
<style>
:root {{ color-scheme: light; --ink: #17202a; --muted: #5c6773; --line: #d8dee7; --panel: #f7f9fc; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: #ffffff; }}
main {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
header {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; }}
h1 {{ margin: 0 0 6px; font-size: 22px; font-weight: 650; }}
.meta {{ color: var(--muted); font-size: 13px; line-height: 1.5; overflow-wrap: anywhere; }}
.summary {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 14px 0; }}
.metric {{ border: 1px solid var(--line); border-radius: 6px; padding: 10px; background: var(--panel); }}
.metric strong {{ display: block; font-size: 18px; margin-top: 4px; }}
.viewer {{ display: grid; grid-template-columns: minmax(0, 1fr) 260px; gap: 18px; align-items: start; }}
.map-wrap {{ border: 1px solid var(--line); border-radius: 6px; padding: 10px; background: #fff; min-width: 0; }}
svg {{ width: 100%; height: auto; display: block; background: #fbfcfe; }}
rect.region {{ stroke: rgba(20, 30, 40, 0.28); stroke-width: 5; vector-effect: non-scaling-stroke; cursor: crosshair; }}
rect.region:hover {{ stroke: #111827; stroke-width: 9; }}
.controls {{ border: 1px solid var(--line); border-radius: 6px; padding: 12px; background: var(--panel); }}
button {{ border: 1px solid #9aa6b2; border-radius: 5px; background: #fff; padding: 7px 10px; cursor: pointer; }}
button:hover {{ background: #eef3f8; }}
input[type="range"] {{ width: 100%; margin: 12px 0; }}
.legend {{ margin: 12px 0 8px; }}
.legend-bar {{ height: 14px; border-radius: 3px; background: linear-gradient(90deg, #313695, #2c7bb6, #00a6ca, #00ccbc, #90eb9d, #ffff8c, #f9d057, #f29e2e, #e76818, #d7191c); border: 1px solid var(--line); }}
.legend-labels {{ display: flex; justify-content: space-between; font-size: 12px; color: var(--muted); }}
#tooltip {{ min-height: 74px; font-size: 13px; line-height: 1.45; overflow-wrap: anywhere; }}
#hotspots {{ margin: 10px 0 0; padding-left: 18px; font-size: 13px; line-height: 1.5; }}
.empty {{ border: 1px dashed var(--line); border-radius: 6px; padding: 28px; color: var(--muted); text-align: center; }}
@media (max-width: 860px) {{ .viewer {{ grid-template-columns: 1fr; }} .summary {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
</style>
</head>
<body>
<main>
<header>
  <div>
    <h1>3D-ICE Runtime Temperature Map</h1>
    <div class="meta">Generated {html.escape(payload["generatedAt"])} from <code>{html.escape(str(tflp_path))}</code></div>
  </div>
</header>
<section class="summary">
  <div class="metric">Rows<strong id="rowCount">0</strong></div>
  <div class="metric">Current Slot<strong id="slotMetric">n/a</strong></div>
  <div class="metric">Minimum<strong id="minMetric">n/a</strong></div>
  <div class="metric">Maximum<strong id="maxMetric">n/a</strong></div>
</section>
<section class="viewer">
  <div class="map-wrap" id="mapContainer"></div>
  <aside class="controls">
    <button id="playButton" type="button">Play</button>
    <input id="slotSlider" type="range" min="0" max="0" value="0" step="1">
    <div class="meta" id="slotText">No rows loaded</div>
    <div class="legend">
      <div class="legend-bar"></div>
      <div class="legend-labels"><span id="legendMin">n/a</span><span id="legendMax">n/a</span></div>
    </div>
    <div id="tooltip">Hover over a region to inspect it.</div>
    <ol id="hotspots"></ol>
  </aside>
</section>
</main>
<script id="temperature-data" type="application/json">{payload_json}</script>
<script>
const data = JSON.parse(document.getElementById('temperature-data').textContent);
const mapContainer = document.getElementById('mapContainer');
const slider = document.getElementById('slotSlider');
const playButton = document.getElementById('playButton');
const slotText = document.getElementById('slotText');
const rowCount = document.getElementById('rowCount');
const slotMetric = document.getElementById('slotMetric');
const minMetric = document.getElementById('minMetric');
const maxMetric = document.getElementById('maxMetric');
const legendMin = document.getElementById('legendMin');
const legendMax = document.getElementById('legendMax');
const tooltip = document.getElementById('tooltip');
const hotspots = document.getElementById('hotspots');
let timer = null;

function fmt(value, digits = 3) {{
  if (!Number.isFinite(value)) return 'n/a';
  return value.toFixed(digits);
}}

function colorFor(value) {{
  const t = Math.max(0, Math.min(1, (value - data.vmin) / (data.vmax - data.vmin)));
  const stops = [
    [49, 54, 149], [44, 123, 182], [0, 166, 202], [0, 204, 188],
    [144, 235, 157], [255, 255, 140], [249, 208, 87], [242, 158, 46],
    [231, 104, 24], [215, 25, 28]
  ];
  const scaled = t * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(scaled));
  const f = scaled - i;
  const a = stops[i];
  const b = stops[i + 1];
  const r = Math.round(a[0] + (b[0] - a[0]) * f);
  const g = Math.round(a[1] + (b[1] - a[1]) * f);
  const bl = Math.round(a[2] + (b[2] - a[2]) * f);
  return `rgb(${{r}},${{g}},${{bl}})`;
}}

function svgY(region) {{
  return data.bounds.maxY + data.bounds.minY - (region.y + region.height);
}}

function buildSvg() {{
  if (!data.regions.length) {{
    mapContainer.innerHTML = '<div class="empty">No floorplan regions loaded.</div>';
    return;
  }}
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `${{data.bounds.minX}} ${{data.bounds.minY}} ${{data.bounds.width}} ${{data.bounds.height}}`);
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', '3D-ICE floorplan temperature map');
  data.regions.forEach((region, index) => {{
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.classList.add('region');
    rect.dataset.index = index;
    rect.setAttribute('x', region.x);
    rect.setAttribute('y', svgY(region));
    rect.setAttribute('width', region.width);
    rect.setAttribute('height', region.height);
    rect.setAttribute('fill', '#d7dde6');
    rect.addEventListener('mouseenter', () => updateTooltip(index, Number(slider.value || 0)));
    rect.addEventListener('mousemove', () => updateTooltip(index, Number(slider.value || 0)));
    const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
    title.textContent = region.name;
    rect.appendChild(title);
    svg.appendChild(rect);
  }});
  mapContainer.replaceChildren(svg);
}}

function updateTooltip(index, slot) {{
  const region = data.regions[index];
  const row = data.rows[slot];
  const temp = row ? row.values[index] : NaN;
  tooltip.innerHTML = `<strong>${{region.name}}</strong><br>Temperature: ${{fmt(temp)}} K<br>Position: ${{fmt(region.x, 1)}}, ${{fmt(region.y, 1)}}<br>Size: ${{fmt(region.width, 1)}} x ${{fmt(region.height, 1)}}`;
}}

function updateHotspots(row) {{
  hotspots.replaceChildren();
  if (!row) return;
  row.values
    .map((value, index) => ({{ value, name: data.regions[index].name }}))
    .sort((a, b) => b.value - a.value)
    .slice(0, 6)
    .forEach(item => {{
      const li = document.createElement('li');
      li.textContent = `${{item.name}}: ${{fmt(item.value)}} K`;
      hotspots.appendChild(li);
    }});
}}

function updateSlot(slot) {{
  const row = data.rows[slot];
  const rects = mapContainer.querySelectorAll('rect.region');
  if (!row) {{
    slotText.textContent = 'Waiting for complete temperature rows.';
    return;
  }}
  let min = Infinity;
  let max = -Infinity;
  row.values.forEach((value, index) => {{
    min = Math.min(min, value);
    max = Math.max(max, value);
    if (rects[index]) rects[index].setAttribute('fill', colorFor(value));
  }});
  slotText.textContent = `Slot ${{slot}} | time ${{fmt(row.time, 6)}} s | updated ${{data.generatedAt}}`;
  slotMetric.textContent = String(slot);
  minMetric.textContent = `${{fmt(min)}} K`;
  maxMetric.textContent = `${{fmt(max)}} K`;
  updateHotspots(row);
}}

function setPlaying(enabled) {{
  if (enabled) {{
    playButton.textContent = 'Pause';
    timer = window.setInterval(() => {{
      const next = Number(slider.value) >= data.rows.length - 1 ? 0 : Number(slider.value) + 1;
      slider.value = String(next);
      updateSlot(next);
    }}, 500);
  }} else {{
    playButton.textContent = 'Play';
    if (timer !== null) window.clearInterval(timer);
    timer = null;
  }}
}}

function init() {{
  rowCount.textContent = String(data.rows.length);
  legendMin.textContent = `${{fmt(data.vmin)}} K`;
  legendMax.textContent = `${{fmt(data.vmax)}} K`;
  buildSvg();
  if (data.rows.length === 0) {{
    slider.disabled = true;
    playButton.disabled = true;
    slotText.textContent = 'Waiting for complete temperature rows.';
    return;
  }}
  slider.max = String(data.rows.length - 1);
  slider.value = String(data.rows.length - 1);
  slider.addEventListener('input', () => updateSlot(Number(slider.value)));
  playButton.addEventListener('click', () => setPlaying(timer === null));
  updateSlot(data.rows.length - 1);
}}

init();
</script>
</body>
</html>
"""


def render_html_once(args, floorplan_path, tflp_path, html_path, refresh_seconds=0.0, allow_empty=False):
    regions = load_floorplan_regions(floorplan_path)
    names, rows = load_tflp_rows(tflp_path, follow=allow_empty)

    if not rows and not allow_empty:
        raise ValueError(f"{tflp_path}: no complete Tflp rows found")

    aligned_regions = align_regions_to_tflp(floorplan_path, regions, names)
    dashboard = build_html_dashboard(
        args,
        floorplan_path,
        tflp_path,
        html_path,
        aligned_regions,
        names,
        rows,
        refresh_seconds,
    )
    write_text_atomic(html_path, dashboard)

    if not args.quiet:
        print(f"Wrote {html_path} with {len(rows)} row(s)", flush=True)

    return len(rows)


def wait_for_path(path, args):
    printed = False

    while not path.exists():
        if not printed and not args.quiet:
            print(f"Waiting for {path} ...", flush=True)
            printed = True
        time.sleep(args.poll)


def render_html_follow(args, floorplan_path, tflp_path, html_path):
    wait_for_path(floorplan_path, args)
    last_signature = None
    refresh_seconds = args.html_refresh if args.html_refresh is not None else args.poll

    while True:
        if tflp_path.exists():
            stat = tflp_path.stat()
            signature = (stat.st_size, stat.st_mtime_ns)

            if signature != last_signature:
                render_html_once(
                    args,
                    floorplan_path,
                    tflp_path,
                    html_path,
                    refresh_seconds=refresh_seconds,
                    allow_empty=True,
                )
                last_signature = signature
        elif not args.quiet:
            print(f"Waiting for {tflp_path} ...", flush=True)

        time.sleep(args.poll)


def run_tmap_mode(args):
    np, plt, PolyCollection = load_plotting_modules(args.backend)

    coords_path = Path(args.coords)
    map_path = Path(args.map)

    geometry = load_geometry(coords_path, np)

    if not args.quiet:
        declared = ""

        if geometry["declared_nrows"] is not None:
            declared = (
                f" (header nrows={geometry['declared_nrows']}, "
                f"ncolumns={geometry['declared_ncolumns']})"
            )

        print(f"Loaded {geometry['cell_count']} geometry cells{declared}", flush=True)

    figure, axis, collection, colorbar = create_figure(args, geometry, np, plt, PolyCollection)
    plt.show(block=False)

    if args.once:
        render_once(args, map_path, geometry, np, plt, figure, axis, collection, colorbar)
    else:
        render_follow(args, map_path, geometry, np, plt, figure, axis, collection, colorbar)


def run_html_mode(args):
    floorplan_path = Path(args.floorplan)
    tflp_path = Path(args.tflp)
    html_path = Path(args.html)

    if args.html_refresh is not None and args.html_refresh < 0.0:
        raise ValueError("--html-refresh must be non-negative")

    if args.once:
        render_html_once(args, floorplan_path, tflp_path, html_path)
    else:
        render_html_follow(args, floorplan_path, tflp_path, html_path)


def main():
    args = parse_args()

    if not args.follow and not args.once:
        args.follow = True

    if args.poll <= 0.0:
        raise ValueError("--poll must be positive")

    mode = validate_mode_args(args)

    if mode == "html":
        run_html_mode(args)
    else:
        run_tmap_mode(args)


def run():
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    run()
