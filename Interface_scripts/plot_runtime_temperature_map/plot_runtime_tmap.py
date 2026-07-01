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

Headless full Tmap dashboard mode:
  python3 plot_runtime_tmap.py --coords COORDS_FILE --map TMAP_FILE --html HTML_FILE

Headless floorplan dashboard mode:
  python3 plot_runtime_tmap.py --floorplan FLOORPLAN_FILE --tflp TFLP_FILE --html HTML_FILE

Animated full Tmap dashboard GIF mode:
  python3 plot_runtime_tmap.py --coords COORDS_FILE --map TMAP_FILE --gif GIF_FILE --once

Animated floorplan dashboard GIF mode:
  python3 plot_runtime_tmap.py --floorplan FLOORPLAN_FILE --tflp TFLP_FILE --gif GIF_FILE --once

Examples:
  python3 plot_runtime_tmap.py --coords xyaxis_TOP_DIE.txt --map output_top_die.txt --html tmap.html --once
  python3 plot_runtime_tmap.py --coords xyaxis_TOP_DIE.txt --map output_top_die.txt --gif tmap.gif --once
  python3 plot_runtime_tmap.py \
      --floorplan runs/default/latest/results/3dice/floorplan_nopower.flp \
      --tflp runs/default/latest/results/3dice/output_top_die_flp_avg.txt \
      --html runs/default/latest/results/3dice/temperature_map.html --once
  python3 plot_runtime_tmap.py \
      --floorplan runs/default/latest/results/3dice/floorplan_nopower.flp \
      --tflp runs/default/latest/results/3dice/output_top_die_flp_avg.txt \
      --gif runs/default/latest/results/3dice/temperature_map.gif --once

Notes:
  - HTML Tmap mode is designed for SSH/headless runs and renders the latest complete Tmap row.
  - HTML floorplan mode is designed for SSH/headless runs and does not need Matplotlib.
  - Follow mode is the default. In HTML mode, follow mode rewrites the HTML file when
    complete rows are appended.
  - GIF modes are offline exports. Full Tmap GIF uses --coords/--map/--gif/--once;
    floorplan GIF uses --floorplan/--tflp/--gif/--once.
  - GIF renders a dashboard-style interface by default; use --gif-layout map for a map-only view.
"""

import argparse
import html
import json
import re
import shutil
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

TURBO_STOPS = [
    (48, 18, 59),
    (65, 69, 170),
    (70, 130, 240),
    (54, 185, 214),
    (70, 210, 130),
    (159, 222, 72),
    (230, 205, 49),
    (249, 141, 38),
    (214, 40, 40),
    (122, 4, 3),
]
COLORBAR_TICK_COUNT = 7
STABLE_TMAP_GEOMETRY_MISMATCH_POLLS = 30


def rgb_css(stop):
    return f"rgb({stop[0]}, {stop[1]}, {stop[2]})"


def turbo_gradient_css():
    return "linear-gradient(90deg, " + ", ".join(rgb_css(stop) for stop in TURBO_STOPS) + ")"


def colorbar_tick_values(vmin, vmax, count=COLORBAR_TICK_COUNT):
    if count <= 1:
        return [vmin]
    return [vmin + (vmax - vmin) * index / (count - 1) for index in range(count)]


def colorbar_labels_html(vmin, vmax):
    return "".join(
        f"<span>{value:.1f} K</span>"
        for value in colorbar_tick_values(vmin, vmax)
    )


def colorbar_html(vmin, vmax):
    return (
        '<div class="map-legend" aria-label="Temperature color scale">'
        '<div class="legend-title">Temperature (K)</div>'
        '<div class="legend-bar"></div>'
        f'<div class="legend-labels">{colorbar_labels_html(vmin, vmax)}</div>'
        '</div>'
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description="Render runtime 3D-ICE temperature maps as an HTML dashboard or GIF."
    )
    parser.add_argument(
        "--coords",
        help="Path to a Tmap coordinate file, for example xyaxis_TOP_DIE.txt.",
    )
    parser.add_argument(
        "--map",
        help="Path to a Tmap temperature file, for example output_top_die.txt.",
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
        help="Path to write a self-contained HTML dashboard for --coords/--map or --floorplan/--tflp mode.",
    )
    parser.add_argument(
        "--gif",
        help="Path to write an animated GIF for --coords/--map or --floorplan/--tflp mode.",
    )
    parser.add_argument("--poll", type=float, default=1.0)
    parser.add_argument("--html-refresh", type=float)
    parser.add_argument("--cmap", default="turbo")
    parser.add_argument("--vmin", type=float)
    parser.add_argument("--vmax", type=float)
    parser.add_argument(
        "--gif-width",
        type=int,
        default=1600,
        help="Output GIF canvas width in pixels.",
    )
    parser.add_argument(
        "--gif-dpi",
        type=float,
        default=120.0,
        help="Matplotlib render DPI for GIF export.",
    )
    parser.add_argument(
        "--gif-fps",
        type=float,
        default=8.0,
        help="Animated GIF playback speed in frames per second.",
    )
    parser.add_argument(
        "--gif-stride",
        type=int,
        default=1,
        help="Render every Nth Tmap or Tflp row; the final row is always included.",
    )
    parser.add_argument(
        "--gif-writer",
        choices=("auto", "imagemagick", "pillow"),
        default="auto",
        help="GIF encoder to use. Auto prefers ImageMagick when available.",
    )
    parser.add_argument(
        "--gif-layout",
        choices=("interface", "map"),
        default="interface",
        help="GIF layout to render. Interface records the dashboard-style view; map renders only the temperature map.",
    )
    parser.add_argument(
        "--gif-labels",
        action="store_true",
        help="Draw floorplan region names inside the GIF.",
    )
    parser.add_argument("--quiet", action="store_true")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--follow", action="store_true")
    mode.add_argument("--once", action="store_true")

    return parser.parse_args()


def validate_mode_args(args):
    floorplan_mode = any(value is not None for value in (args.floorplan, args.tflp))
    tmap_mode = any(value is not None for value in (args.coords, args.map))

    if floorplan_mode and tmap_mode:
        raise ValueError("choose either --floorplan/--tflp output mode or --coords/--map mode, not both")

    if floorplan_mode:
        missing = [name for name, value in (("--floorplan", args.floorplan), ("--tflp", args.tflp)) if value is None]
        if missing:
            raise ValueError(f"floorplan output mode requires {', '.join(missing)}")
        if args.html is None and args.gif is None:
            raise ValueError("floorplan output mode requires --html and/or --gif")
        return "floorplan"

    if tmap_mode:
        missing = [name for name, value in (("--coords", args.coords), ("--map", args.map)) if value is None]
        if missing:
            raise ValueError(f"Tmap mode requires {', '.join(missing)}")
        if args.html is None and args.gif is None:
            raise ValueError("Tmap mode requires --html and/or --gif")
        return "tmap"

    if args.html is not None:
        raise ValueError("--html requires either --coords/--map or --floorplan/--tflp")

    if args.gif is not None:
        raise ValueError("--gif requires either --coords/--map or --floorplan/--tflp")

    raise ValueError("provide either --floorplan/--tflp output mode or --coords/--map mode")


def load_gif_modules():
    try:
        import numpy as np
        import matplotlib

        matplotlib.use("Agg")

        import matplotlib.pyplot as plt
        from matplotlib.animation import FuncAnimation, ImageMagickWriter, PillowWriter
        from matplotlib.collections import PatchCollection
        from matplotlib.colors import Normalize
        from matplotlib.patches import Rectangle
    except ImportError as exc:
        print(
            "ERROR: missing Python GIF plotting dependency.\n"
            "Install GIF plotting dependencies with:\n"
            "  python3 -m pip install -r Interface_scripts/plot_runtime_temperature_map/requirements_tmap_plot.txt\n"
            f"Original import error: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    return np, plt, FuncAnimation, ImageMagickWriter, PillowWriter, PatchCollection, Normalize, Rectangle


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

        if first_byte is None or first_byte in (ord("#"), ord("%")):
            continue

        return raw_line, line_number


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
        "turboStops": TURBO_STOPS,
    }
    payload_json = json.dumps(payload, separators=(",", ":"))
    refresh_tag = ""

    if refresh_seconds and refresh_seconds > 0.0:
        refresh_tag = f'<meta http-equiv="refresh" content="{html.escape(html_number(refresh_seconds))}">'

    template = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
__REFRESH_TAG__
<title>3D-ICE Runtime Temperature Map</title>
<style>
:root { color-scheme: light; --ink: #17202a; --muted: #5c6773; --line: #d8dee7; --panel: #f7f9fc; }
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: #ffffff; }
main { max-width: 1200px; margin: 0 auto; padding: 20px; }
header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
h1 { margin: 0 0 6px; font-size: 22px; font-weight: 650; }
.meta { color: var(--muted); font-size: 13px; line-height: 1.5; overflow-wrap: anywhere; }
.summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 14px 0; }
.metric { border: 1px solid var(--line); border-radius: 6px; padding: 10px; background: var(--panel); }
.metric strong { display: block; font-size: 20px; margin-top: 4px; }
.viewer { display: grid; grid-template-columns: minmax(0, 1fr) 260px; gap: 18px; align-items: start; }
.map-wrap { border: 1px solid var(--line); border-radius: 6px; padding: 10px; background: #fff; min-width: 0; }
svg { width: 100%; height: auto; display: block; background: #fbfcfe; }
rect.region { stroke: rgba(20, 30, 40, 0.28); stroke-width: 5; vector-effect: non-scaling-stroke; cursor: crosshair; }
rect.region:hover { stroke: #111827; stroke-width: 9; }
.controls { border: 1px solid var(--line); border-radius: 6px; padding: 12px; background: var(--panel); }
button { border: 1px solid #9aa6b2; border-radius: 5px; background: #fff; padding: 7px 10px; cursor: pointer; }
button:hover { background: #eef3f8; }
input[type="range"] { width: 100%; margin: 12px 0; }
.map-legend { margin-top: 10px; }
.legend-title { margin-bottom: 6px; color: var(--ink); font-size: 14px; font-weight: 650; }
.legend-bar { height: 16px; border-radius: 3px; background: __TURBO_GRADIENT__; border: 1px solid var(--line); }
.legend-labels { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 4px; margin-top: 5px; font-size: 13px; color: var(--muted); }
.legend-labels span { text-align: center; white-space: nowrap; }
.legend-labels span:first-child { text-align: left; }
.legend-labels span:last-child { text-align: right; }
#tooltip { min-height: 74px; font-size: 15px; line-height: 1.45; overflow-wrap: anywhere; }
#hotspots { margin: 10px 0 0; padding-left: 18px; font-size: 15px; line-height: 1.5; }
.empty { border: 1px dashed var(--line); border-radius: 6px; padding: 28px; color: var(--muted); text-align: center; }
@media (max-width: 860px) { .viewer { grid-template-columns: 1fr; } .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
</head>
<body>
<main>
<header>
  <div>
    <h1>3D-ICE Runtime Temperature Map</h1>
    <div class="meta">Generated __GENERATED_AT__ from <code>__TFLP_PATH__</code></div>
  </div>
</header>
<section class="summary">
  <div class="metric">Rows<strong id="rowCount">0</strong></div>
  <div class="metric">Current Slot<strong id="slotMetric">n/a</strong></div>
  <div class="metric">Minimum<strong id="minMetric">n/a</strong></div>
  <div class="metric">Maximum<strong id="maxMetric">n/a</strong></div>
</section>
<section class="viewer">
  <div class="map-wrap"><div id="mapContainer"></div>__COLORBAR_HTML__</div>
  <aside class="controls">
    <button id="playButton" type="button">Play</button>
    <input id="slotSlider" type="range" min="0" max="0" value="0" step="1">
    <div class="meta" id="slotText">No rows loaded</div>
    <div id="tooltip">Hover over a region to inspect it.</div>
    <ol id="hotspots"></ol>
  </aside>
</section>
</main>
<script id="temperature-data" type="application/json">__PAYLOAD_JSON__</script>
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
const tooltip = document.getElementById('tooltip');
const hotspots = document.getElementById('hotspots');
let timer = null;

function fmt(value, digits = 1) {
  if (!Number.isFinite(value)) return 'n/a';
  return value.toFixed(digits);
}

function colorFor(value) {
  const t = Math.max(0, Math.min(1, (value - data.vmin) / (data.vmax - data.vmin)));
  const stops = data.turboStops;
  const scaled = t * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(scaled));
  const f = scaled - i;
  const a = stops[i];
  const b = stops[i + 1];
  const r = Math.round(a[0] + (b[0] - a[0]) * f);
  const g = Math.round(a[1] + (b[1] - a[1]) * f);
  const bl = Math.round(a[2] + (b[2] - a[2]) * f);
  return `rgb(${r},${g},${bl})`;
}

function svgY(region) {
  return data.bounds.maxY + data.bounds.minY - (region.y + region.height);
}

function buildSvg() {
  if (!data.regions.length) {
    mapContainer.innerHTML = '<div class="empty">No floorplan regions loaded.</div>';
    return;
  }
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `${data.bounds.minX} ${data.bounds.minY} ${data.bounds.width} ${data.bounds.height}`);
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', '3D-ICE floorplan temperature map');
  data.regions.forEach((region, index) => {
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
  });
  mapContainer.replaceChildren(svg);
}

function updateTooltip(index, slot) {
  const region = data.regions[index];
  const row = data.rows[slot];
  const temp = row ? row.values[index] : NaN;
  tooltip.innerHTML = `<strong>${region.name}</strong><br>Temperature: ${fmt(temp)} K<br>Position: ${fmt(region.x, 1)}, ${fmt(region.y, 1)}<br>Size: ${fmt(region.width, 1)} x ${fmt(region.height, 1)}`;
}

function updateHotspots(row) {
  hotspots.replaceChildren();
  if (!row) return;
  row.values
    .map((value, index) => ({ value, name: data.regions[index].name }))
    .sort((a, b) => b.value - a.value)
    .slice(0, 6)
    .forEach(item => {
      const li = document.createElement('li');
      li.textContent = `${item.name}: ${fmt(item.value)} K`;
      hotspots.appendChild(li);
    });
}

function updateSlot(slot) {
  const row = data.rows[slot];
  const rects = mapContainer.querySelectorAll('rect.region');
  if (!row) {
    slotText.textContent = 'Waiting for complete temperature rows.';
    return;
  }
  let min = Infinity;
  let max = -Infinity;
  row.values.forEach((value, index) => {
    min = Math.min(min, value);
    max = Math.max(max, value);
    if (rects[index]) rects[index].setAttribute('fill', colorFor(value));
  });
  const displaySlot = slot + 1;
  slotText.textContent = `Slot ${displaySlot} / ${data.rows.length} | time ${fmt(row.time, 6)} s | updated ${data.generatedAt}`;
  slotMetric.textContent = String(displaySlot);
  minMetric.textContent = `${fmt(min)} K`;
  maxMetric.textContent = `${fmt(max)} K`;
  updateHotspots(row);
}

function setPlaying(enabled) {
  if (enabled) {
    playButton.textContent = 'Pause';
    timer = window.setInterval(() => {
      const next = Number(slider.value) >= data.rows.length - 1 ? 0 : Number(slider.value) + 1;
      slider.value = String(next);
      updateSlot(next);
    }, 500);
  } else {
    playButton.textContent = 'Play';
    if (timer !== null) window.clearInterval(timer);
    timer = null;
  }
}

function init() {
  rowCount.textContent = String(data.rows.length);
  buildSvg();
  if (data.rows.length === 0) {
    slider.disabled = true;
    playButton.disabled = true;
    slotText.textContent = 'Waiting for complete temperature rows.';
    return;
  }
  slider.max = String(data.rows.length - 1);
  slider.value = String(data.rows.length - 1);
  slider.addEventListener('input', () => updateSlot(Number(slider.value)));
  playButton.addEventListener('click', () => setPlaying(timer === null));
  updateSlot(data.rows.length - 1);
}

init();
</script>
</body>
</html>
"""
    return (
        template.replace("__REFRESH_TAG__", refresh_tag)
        .replace("__TURBO_GRADIENT__", turbo_gradient_css())
        .replace("__GENERATED_AT__", html.escape(payload["generatedAt"]))
        .replace("__TFLP_PATH__", html.escape(str(tflp_path)))
        .replace("__COLORBAR_HTML__", colorbar_html(vmin, vmax))
        .replace("__PAYLOAD_JSON__", payload_json)
    )

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


def validate_gif_args(args):
    if args.gif_width < 320:
        raise ValueError("--gif-width must be at least 320 pixels")
    if args.gif_dpi <= 0.0:
        raise ValueError("--gif-dpi must be positive")
    if args.gif_fps <= 0.0:
        raise ValueError("--gif-fps must be positive")
    if args.gif_stride < 1:
        raise ValueError("--gif-stride must be at least 1")


def frame_indices_for_rows(row_count, stride):
    indices = list(range(0, row_count, stride))

    if indices and indices[-1] != row_count - 1:
        indices.append(row_count - 1)

    return indices


def make_gif_writer(args, ImageMagickWriter, PillowWriter):
    writer_name = args.gif_writer

    if writer_name == "auto":
        writer_name = "imagemagick" if shutil.which("convert") or shutil.which("magick") else "pillow"

    if writer_name == "imagemagick":
        return ImageMagickWriter(fps=args.gif_fps), writer_name

    return PillowWriter(fps=args.gif_fps), writer_name


def save_animation_atomic(animation, gif_path, writer, dpi):
    gif_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = gif_path.with_name(f".{gif_path.name}.tmp.gif")

    try:
        animation.save(temp_path, writer=writer, dpi=dpi)
        temp_path.replace(gif_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def prepare_gif_data(args, floorplan_path, tflp_path, np):
    regions = load_floorplan_regions(floorplan_path)
    names, rows = load_tflp_rows(tflp_path, follow=False)

    if not rows:
        raise ValueError(f"{tflp_path}: no complete Tflp rows found")

    regions = align_regions_to_tflp(floorplan_path, regions, names)
    frame_indices = frame_indices_for_rows(len(rows), args.gif_stride)
    bounds = floorplan_bounds(regions)
    values = np.asarray(
        [temperature for row in rows for temperature in row["values"]],
        dtype=np.float64,
    )
    data_min = float(values.min())
    data_max = float(values.max())
    vmin = args.vmin if args.vmin is not None else data_min
    vmax = args.vmax if args.vmax is not None else data_max

    if vmax <= vmin:
        vmax = vmin + 1.0

    return {
        "regions": regions,
        "names": names,
        "rows": rows,
        "frame_indices": frame_indices,
        "bounds": bounds,
        "data_min": data_min,
        "data_max": data_max,
        "vmin": vmin,
        "vmax": vmax,
    }


def save_gif_with_fallback(args, plt, figure, animation, gif_path, ImageMagickWriter, PillowWriter):
    writer, writer_name = make_gif_writer(args, ImageMagickWriter, PillowWriter)

    try:
        try:
            save_animation_atomic(animation, gif_path, writer, args.gif_dpi)
        except Exception as exc:
            if args.gif_writer != "auto" or writer_name != "imagemagick":
                raise
            if not args.quiet:
                print(f"ImageMagick GIF writer failed ({exc}); retrying with Pillow.", flush=True)
            writer = PillowWriter(fps=args.gif_fps)
            save_animation_atomic(animation, gif_path, writer, args.gif_dpi)
            writer_name = "pillow"

        width_px, height_px = figure.canvas.get_width_height()
    finally:
        plt.close(figure)

    return writer_name, width_px, height_px


def add_figure_panel(figure, x, y, width, height, facecolor="#ffffff", edgecolor="#d8dee7"):
    from matplotlib.patches import Rectangle

    panel = Rectangle(
        (x, y),
        width,
        height,
        transform=figure.transFigure,
        facecolor=facecolor,
        edgecolor=edgecolor,
        linewidth=0.8,
        zorder=0,
    )
    figure.patches.append(panel)
    return panel


def add_metric_card(figure, x, y, width, height, label, initial_value):
    add_figure_panel(figure, x, y, width, height, facecolor="#f7f9fc")
    figure.text(
        x + 0.012,
        y + height - 0.027,
        label,
        color="#5c6773",
        fontsize=9.5,
        va="top",
        zorder=4,
    )
    return figure.text(
        x + 0.012,
        y + 0.020,
        initial_value,
        color="#17202a",
        fontsize=15,
        fontweight="semibold",
        va="bottom",
        zorder=4,
    )


def shorten_text(value, max_chars):
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3] + "..."


def format_gif_temp(value):
    return f"{value:.1f} K"


def render_gif_map_layout(args, gif_data, modules, gif_path):
    (
        np,
        plt,
        FuncAnimation,
        ImageMagickWriter,
        PillowWriter,
        PatchCollection,
        Normalize,
        Rectangle,
    ) = modules

    regions = gif_data["regions"]
    rows = gif_data["rows"]
    frame_indices = gif_data["frame_indices"]
    bounds = gif_data["bounds"]
    vmin = gif_data["vmin"]
    vmax = gif_data["vmax"]

    figure_width = args.gif_width / args.gif_dpi
    figure_height = max(4.0, figure_width * bounds["height"] / bounds["width"] * 0.82)
    figure, axis = plt.subplots(
        figsize=(figure_width, figure_height),
        dpi=args.gif_dpi,
        constrained_layout=True,
    )
    axis.set_aspect("equal", adjustable="box")
    axis.set_xlim(bounds["minX"], bounds["maxX"])
    axis.set_ylim(bounds["minY"], bounds["maxY"])
    axis.set_xlabel("x")
    axis.set_ylabel("y")

    patches = [
        Rectangle(
            (region["x"], region["y"]),
            region["width"],
            region["height"],
        )
        for region in regions
    ]
    norm = Normalize(vmin=vmin, vmax=vmax)
    collection = PatchCollection(
        patches,
        cmap=plt.get_cmap(args.cmap),
        norm=norm,
        edgecolors=(0.08, 0.1, 0.13, 0.35),
        linewidths=0.45,
        antialiased=False,
    )
    axis.add_collection(collection)
    colorbar = figure.colorbar(collection, ax=axis, fraction=0.046, pad=0.04)
    colorbar.set_label("Temperature (K)", fontsize=11)
    colorbar.ax.tick_params(labelsize=9)

    if args.gif_labels:
        label_size = max(4.5, min(8.0, args.gif_width / 250.0))
        for region in regions:
            axis.text(
                region["x"] + region["width"] / 2.0,
                region["y"] + region["height"] / 2.0,
                region["name"],
                ha="center",
                va="center",
                fontsize=label_size,
                color="#111827",
                clip_on=True,
            )

    title = axis.set_title("")

    def update(frame_index):
        row = rows[frame_index]
        temperatures = np.asarray(row["values"], dtype=np.float64)
        min_temp = float(temperatures.min())
        max_temp = float(temperatures.max())
        collection.set_array(temperatures)
        title.set_text(
            f"Slot {frame_index + 1}/{len(rows)} | time {row['time']:.6g} s | "
            f"min {min_temp:.1f} K | max {max_temp:.1f} K"
        )
        return collection, title

    update(frame_indices[0])
    animation = FuncAnimation(
        figure,
        update,
        frames=frame_indices,
        interval=1000.0 / args.gif_fps,
        blit=False,
        repeat=True,
        cache_frame_data=False,
    )
    return save_gif_with_fallback(
        args,
        plt,
        figure,
        animation,
        gif_path,
        ImageMagickWriter,
        PillowWriter,
    )


def render_gif_interface_layout(args, floorplan_path, tflp_path, gif_data, modules, gif_path):
    (
        np,
        plt,
        FuncAnimation,
        ImageMagickWriter,
        PillowWriter,
        PatchCollection,
        Normalize,
        Rectangle,
    ) = modules

    regions = gif_data["regions"]
    rows = gif_data["rows"]
    frame_indices = gif_data["frame_indices"]
    bounds = gif_data["bounds"]
    vmin = gif_data["vmin"]
    vmax = gif_data["vmax"]
    cmap = plt.get_cmap(args.cmap)
    norm = Normalize(vmin=vmin, vmax=vmax)

    figure_width = args.gif_width / args.gif_dpi
    figure_height = max(4.8, figure_width * 0.62)
    figure = plt.figure(figsize=(figure_width, figure_height), dpi=args.gif_dpi)
    figure.patch.set_facecolor("#ffffff")

    short_tflp_path = shorten_text(str(tflp_path), 120)
    figure.text(
        0.045,
        0.965,
        "3D-ICE Runtime Temperature Map",
        color="#17202a",
        fontsize=16,
        fontweight="semibold",
        va="top",
    )
    figure.text(
        0.045,
        0.930,
        f"Animated dashboard from {short_tflp_path}",
        color="#5c6773",
        fontsize=8.5,
        va="top",
    )

    card_y = 0.815
    card_h = 0.086
    card_gap = 0.012
    card_x = 0.045
    card_total_w = 0.910
    card_w = (card_total_w - 3 * card_gap) / 4.0
    row_count_text = add_metric_card(figure, card_x, card_y, card_w, card_h, "Rows", str(len(rows)))
    slot_metric_text = add_metric_card(
        figure,
        card_x + (card_w + card_gap),
        card_y,
        card_w,
        card_h,
        "Current Slot",
        "n/a",
    )
    min_metric_text = add_metric_card(
        figure,
        card_x + 2 * (card_w + card_gap),
        card_y,
        card_w,
        card_h,
        "Minimum",
        "n/a",
    )
    max_metric_text = add_metric_card(
        figure,
        card_x + 3 * (card_w + card_gap),
        card_y,
        card_w,
        card_h,
        "Maximum",
        "n/a",
    )

    add_figure_panel(figure, 0.045, 0.115, 0.665, 0.665, facecolor="#ffffff")
    add_figure_panel(figure, 0.735, 0.115, 0.220, 0.665, facecolor="#f7f9fc")

    map_axis = figure.add_axes([0.065, 0.250, 0.625, 0.485], zorder=2)
    map_axis.set_aspect("equal", adjustable="box")
    map_axis.set_xlim(bounds["minX"], bounds["maxX"])
    map_axis.set_ylim(bounds["minY"], bounds["maxY"])
    map_axis.set_xticks([])
    map_axis.set_yticks([])
    map_axis.set_facecolor("#fbfcfe")
    for spine in map_axis.spines.values():
        spine.set_edgecolor("#d8dee7")
        spine.set_linewidth(0.8)

    patches = [
        Rectangle(
            (region["x"], region["y"]),
            region["width"],
            region["height"],
        )
        for region in regions
    ]
    collection = PatchCollection(
        patches,
        cmap=cmap,
        norm=norm,
        edgecolors=(0.08, 0.1, 0.13, 0.35),
        linewidths=0.45,
        antialiased=False,
    )
    map_axis.add_collection(collection)

    if args.gif_labels:
        label_size = max(4.0, min(7.0, args.gif_width / 270.0))
        for region in regions:
            map_axis.text(
                region["x"] + region["width"] / 2.0,
                region["y"] + region["height"] / 2.0,
                shorten_text(region["name"], 16),
                ha="center",
                va="center",
                fontsize=label_size,
                color="#111827",
                clip_on=True,
            )

    colorbar_axis = figure.add_axes([0.065, 0.165, 0.625, 0.030], zorder=2)
    gradient = np.linspace(vmin, vmax, 512, dtype=np.float64).reshape(1, -1)
    colorbar_axis.imshow(
        gradient,
        aspect="auto",
        cmap=cmap,
        norm=norm,
        extent=(vmin, vmax, 0.0, 1.0),
    )
    colorbar_axis.set_yticks([])
    figure.text(
        0.065,
        0.209,
        "Temperature (K)",
        color="#17202a",
        fontsize=10,
        fontweight="semibold",
        ha="left",
        va="bottom",
        zorder=4,
    )
    ticks = colorbar_tick_values(vmin, vmax)
    colorbar_axis.set_xticks(ticks)
    colorbar_axis.set_xticklabels([f"{tick:.4g} K" for tick in ticks], fontsize=8.5)
    colorbar_axis.tick_params(axis="x", length=0, pad=3, colors="#5c6773")
    for spine in colorbar_axis.spines.values():
        spine.set_edgecolor("#d8dee7")
        spine.set_linewidth(0.8)

    side_axis = figure.add_axes([0.735, 0.115, 0.220, 0.665], zorder=2)
    side_axis.set_xlim(0.0, 1.0)
    side_axis.set_ylim(0.0, 1.0)
    side_axis.axis("off")

    side_axis.add_patch(
        Rectangle((0.065, 0.905), 0.235, 0.058, facecolor="#ffffff", edgecolor="#9aa6b2", linewidth=0.8)
    )
    side_axis.text(0.182, 0.934, "Play", color="#17202a", fontsize=8.5, ha="center", va="center")
    side_axis.add_patch(
        Rectangle((0.065, 0.835), 0.870, 0.030, facecolor="#d8dee7", edgecolor="#c9d2dc", linewidth=0.5)
    )
    progress_fill = Rectangle((0.065, 0.835), 0.0, 0.030, facecolor="#3478f6", edgecolor="none")
    side_axis.add_patch(progress_fill)
    slot_text = side_axis.text(
        0.065,
        0.790,
        "",
        color="#5c6773",
        fontsize=8.8,
        ha="left",
        va="top",
        wrap=True,
    )

    side_axis.add_patch(
        Rectangle((0.065, 0.600), 0.870, 0.145, facecolor="#ffffff", edgecolor="#d8dee7", linewidth=0.8)
    )
    selected_text = side_axis.text(
        0.090,
        0.720,
        "",
        color="#17202a",
        fontsize=8.8,
        ha="left",
        va="top",
        linespacing=1.35,
        wrap=True,
    )
    side_axis.text(
        0.065,
        0.535,
        "Hottest floorplan elements",
        color="#17202a",
        fontsize=10.0,
        fontweight="semibold",
        ha="left",
        va="top",
    )
    hotspot_texts = [
        side_axis.text(
            0.075,
            0.490 - index * 0.060,
            "",
            color="#17202a",
            fontsize=8.8,
            ha="left",
            va="top",
        )
        for index in range(6)
    ]

    def update(frame_index):
        row = rows[frame_index]
        temperatures = np.asarray(row["values"], dtype=np.float64)
        min_temp = float(temperatures.min())
        max_temp = float(temperatures.max())
        collection.set_array(temperatures)

        display_slot = frame_index + 1
        slot_metric_text.set_text(str(display_slot))
        min_metric_text.set_text(format_gif_temp(min_temp))
        max_metric_text.set_text(format_gif_temp(max_temp))
        progress = 1.0 if len(rows) <= 1 else frame_index / (len(rows) - 1)
        progress_fill.set_width(0.870 * progress)
        slot_text.set_text(f"Slot {display_slot} / {len(rows)} | time {row['time']:.6g} s")

        hottest_indices = np.argsort(temperatures)[::-1][:6]
        hottest_index = int(hottest_indices[0])
        hottest_region = regions[hottest_index]
        selected_text.set_text(
            f"{shorten_text(hottest_region['name'], 28)}\n"
            f"Temperature: {format_gif_temp(float(temperatures[hottest_index]))}\n"
            f"Position: {hottest_region['x']:.3g}, {hottest_region['y']:.3g}\n"
            f"Size: {hottest_region['width']:.3g} x {hottest_region['height']:.3g}"
        )
        for rank, text in enumerate(hotspot_texts):
            if rank >= len(hottest_indices):
                text.set_text("")
                continue
            index = int(hottest_indices[rank])
            name = shorten_text(regions[index]["name"], 22)
            text.set_text(f"{rank + 1}. {name}: {format_gif_temp(float(temperatures[index]))}")

        return (
            collection,
            row_count_text,
            slot_metric_text,
            min_metric_text,
            max_metric_text,
            progress_fill,
            slot_text,
            selected_text,
            *hotspot_texts,
        )

    update(frame_indices[0])
    animation = FuncAnimation(
        figure,
        update,
        frames=frame_indices,
        interval=1000.0 / args.gif_fps,
        blit=False,
        repeat=True,
        cache_frame_data=False,
    )
    return save_gif_with_fallback(
        args,
        plt,
        figure,
        animation,
        gif_path,
        ImageMagickWriter,
        PillowWriter,
    )


def render_gif_once(args, floorplan_path, tflp_path, gif_path):
    validate_gif_args(args)

    modules = load_gif_modules()
    np = modules[0]
    gif_data = prepare_gif_data(args, floorplan_path, tflp_path, np)

    if args.gif_layout == "map":
        writer_name, width_px, height_px = render_gif_map_layout(args, gif_data, modules, gif_path)
    else:
        writer_name, width_px, height_px = render_gif_interface_layout(
            args,
            floorplan_path,
            tflp_path,
            gif_data,
            modules,
            gif_path,
        )

    if not args.quiet:
        print(
            f"Wrote {gif_path} with {len(gif_data['frame_indices'])} frame(s) from {len(gif_data['rows'])} row(s) "
            f"at {width_px}x{height_px}px, {args.gif_fps:g} fps using {writer_name} "
            f"({args.gif_layout} layout)",
            flush=True,
        )

    return len(gif_data["frame_indices"])


def load_tmap_geometry_cells(coords_path, follow=False, allow_empty=False):
    cells = []
    min_x = None
    min_y = None
    max_x = None
    max_y = None
    line_number = 0

    with coords_path.open("rb") as stream:
        while True:
            raw_line, line_number = read_complete_data_line(stream, line_number, follow=follow)

            if raw_line is None:
                break

            parts = raw_line.split()
            if len(parts) != 4:
                raise ValueError(f"{coords_path}:{line_number}: expected 4 coordinate fields, got {len(parts)}")

            x, y, width, height = map(float, parts)
            if width <= 0.0 or height <= 0.0:
                raise ValueError(f"{coords_path}:{line_number}: non-positive cell dimensions")

            cells.append((x, y, width, height))
            min_x = x if min_x is None else min(min_x, x)
            min_y = y if min_y is None else min(min_y, y)
            max_x = x + width if max_x is None else max(max_x, x + width)
            max_y = y + height if max_y is None else max(max_y, y + height)

    if not cells:
        if allow_empty:
            return None
        raise ValueError(f"{coords_path}: no coordinate cells found")

    return {
        "cells": cells,
        "cell_count": len(cells),
        "bounds": {
            "minX": min_x,
            "minY": min_y,
            "maxX": max_x,
            "maxY": max_y,
            "width": max_x - min_x,
            "height": max_y - min_y,
        },
    }


def read_first_tmap_row_width(map_path, follow=False):
    line_number = 0

    with map_path.open("rb") as stream:
        raw_line, line_number = read_complete_data_line(stream, line_number, follow=follow)

    if raw_line is None:
        return None, line_number

    return len(raw_line.split()), line_number


def parse_tmap_values(raw_line, expected_count, map_path, line_number):
    values = [float(part) for part in raw_line.split()]

    if len(values) != expected_count:
        raise ValueError(f"{map_path}:{line_number}: expected {expected_count} temperatures, got {len(values)}")

    return values


def read_latest_existing_tmap_values(map_path, expected_count):
    latest_raw_line = None
    latest_line_number = None
    row_count = 0
    line_number = 0

    with map_path.open("rb") as stream:
        while True:
            raw_line, line_number = read_complete_data_line(stream, line_number, follow=False)

            if raw_line is None:
                break

            latest_raw_line = raw_line
            latest_line_number = line_number
            row_count += 1

    if latest_raw_line is None:
        return None, -1, 0

    return parse_tmap_values(latest_raw_line, expected_count, map_path, latest_line_number), row_count - 1, row_count



def read_existing_tmap_slots(map_path, expected_count, follow=False):
    slots = []
    data_min = None
    data_max = None
    row_count = 0
    line_number = 0

    with map_path.open("rb") as stream:
        while True:
            raw_line, line_number = read_complete_data_line(stream, line_number, follow=follow)

            if raw_line is None:
                break

            values = parse_tmap_values(raw_line, expected_count, map_path, line_number)
            row_min = min(values)
            row_max = max(values)
            data_min = row_min if data_min is None else min(data_min, row_min)
            data_max = row_max if data_max is None else max(data_max, row_max)
            slots.append({"slot": row_count, "values": values})
            row_count += 1

    return slots, row_count, data_min, data_max


def tmap_geometry_vertices(geometry, np):
    cells_array = np.asarray(geometry["cells"], dtype=np.float64)
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
    return vertices


def read_tmap_gif_stats(map_path, expected_count, np):
    data_min = None
    data_max = None
    row_count = 0
    line_number = 0

    with map_path.open("rb") as stream:
        while True:
            raw_line, line_number = read_complete_data_line(stream, line_number, follow=False)

            if raw_line is None:
                break

            temperatures = parse_temperature_row(raw_line, expected_count, map_path, line_number, np)
            row_min = float(temperatures.min())
            row_max = float(temperatures.max())
            data_min = row_min if data_min is None else min(data_min, row_min)
            data_max = row_max if data_max is None else max(data_max, row_max)
            row_count += 1

    if row_count == 0:
        raise ValueError(f"{map_path}: no complete Tmap rows found")

    return row_count, data_min, data_max


def read_tmap_gif_frame_rows(map_path, expected_count, frame_indices, np):
    wanted = set(frame_indices)
    frame_rows = []
    row_index = 0
    line_number = 0

    with map_path.open("rb") as stream:
        while True:
            raw_line, line_number = read_complete_data_line(stream, line_number, follow=False)

            if raw_line is None:
                break

            temperatures = parse_temperature_row(raw_line, expected_count, map_path, line_number, np)

            if row_index in wanted:
                frame_rows.append({"slot": row_index, "values": temperatures})

            row_index += 1

    if len(frame_rows) != len(frame_indices):
        raise ValueError(
            f"{map_path}: expected {len(frame_indices)} GIF frame row(s), got {len(frame_rows)}"
        )

    return frame_rows


def prepare_tmap_gif_data(args, coords_path, map_path, np):
    geometry = load_tmap_geometry_cells(coords_path)
    row_count, data_min, data_max = read_tmap_gif_stats(
        map_path,
        geometry["cell_count"],
        np,
    )
    frame_indices = frame_indices_for_rows(row_count, args.gif_stride)
    frame_rows = read_tmap_gif_frame_rows(
        map_path,
        geometry["cell_count"],
        frame_indices,
        np,
    )
    vmin = args.vmin if args.vmin is not None else data_min
    vmax = args.vmax if args.vmax is not None else data_max

    if vmax <= vmin:
        vmax = vmin + 1.0

    return {
        "geometry": geometry,
        "row_count": row_count,
        "frame_indices": frame_indices,
        "frame_rows": frame_rows,
        "data_min": data_min,
        "data_max": data_max,
        "vmin": vmin,
        "vmax": vmax,
    }


def add_tmap_poly_collection(args, gif_data, np, plt, axis, Normalize):
    from matplotlib.collections import PolyCollection

    collection = PolyCollection(
        tmap_geometry_vertices(gif_data["geometry"], np),
        array=np.zeros(gif_data["geometry"]["cell_count"], dtype=np.float64),
        cmap=plt.get_cmap(args.cmap),
        norm=Normalize(vmin=gif_data["vmin"], vmax=gif_data["vmax"]),
        edgecolors="none",
        linewidths=0,
        antialiased=False,
        rasterized=True,
    )
    axis.add_collection(collection)
    return collection


def render_tmap_gif_map_layout(args, gif_data, modules, gif_path):
    (
        np,
        plt,
        FuncAnimation,
        ImageMagickWriter,
        PillowWriter,
        PatchCollection,
        Normalize,
        Rectangle,
    ) = modules

    bounds = gif_data["geometry"]["bounds"]
    figure_width = args.gif_width / args.gif_dpi
    figure_height = max(4.0, figure_width * bounds["height"] / bounds["width"] * 0.82)
    figure, axis = plt.subplots(
        figsize=(figure_width, figure_height),
        dpi=args.gif_dpi,
        constrained_layout=True,
    )
    axis.set_aspect("equal", adjustable="box")
    axis.set_xlim(bounds["minX"], bounds["maxX"])
    axis.set_ylim(bounds["minY"], bounds["maxY"])
    axis.set_xlabel("x")
    axis.set_ylabel("y")

    collection = add_tmap_poly_collection(args, gif_data, np, plt, axis, Normalize)
    colorbar = figure.colorbar(collection, ax=axis, fraction=0.046, pad=0.04)
    colorbar.set_label("Temperature (K)", fontsize=11)
    colorbar.ax.tick_params(labelsize=9)
    title = axis.set_title("")

    def update(frame_row):
        temperatures = frame_row["values"]
        min_temp = float(temperatures.min())
        max_temp = float(temperatures.max())
        collection.set_array(temperatures)
        title.set_text(
            f"Slot {frame_row['slot'] + 1}/{gif_data['row_count']} | "
            f"min {min_temp:.1f} K | max {max_temp:.1f} K"
        )
        return collection, title

    update(gif_data["frame_rows"][0])
    animation = FuncAnimation(
        figure,
        update,
        frames=gif_data["frame_rows"],
        interval=1000.0 / args.gif_fps,
        blit=False,
        repeat=True,
        cache_frame_data=False,
    )
    return save_gif_with_fallback(
        args,
        plt,
        figure,
        animation,
        gif_path,
        ImageMagickWriter,
        PillowWriter,
    )


def render_tmap_gif_interface_layout(args, coords_path, map_path, gif_data, modules, gif_path):
    (
        np,
        plt,
        FuncAnimation,
        ImageMagickWriter,
        PillowWriter,
        PatchCollection,
        Normalize,
        Rectangle,
    ) = modules

    geometry = gif_data["geometry"]
    bounds = geometry["bounds"]
    cells = geometry["cells"]
    cmap = plt.get_cmap(args.cmap)
    norm = Normalize(vmin=gif_data["vmin"], vmax=gif_data["vmax"])

    figure_width = args.gif_width / args.gif_dpi
    figure_height = max(4.8, figure_width * 0.62)
    figure = plt.figure(figsize=(figure_width, figure_height), dpi=args.gif_dpi)
    figure.patch.set_facecolor("#ffffff")

    short_map_path = shorten_text(str(map_path), 120)
    figure.text(
        0.045,
        0.965,
        "3D-ICE Tmap",
        color="#17202a",
        fontsize=16,
        fontweight="semibold",
        va="top",
    )
    figure.text(
        0.045,
        0.930,
        f"Animated full Tmap dashboard from {short_map_path}",
        color="#5c6773",
        fontsize=8.5,
        va="top",
    )

    card_y = 0.815
    card_h = 0.086
    card_gap = 0.012
    card_x = 0.045
    card_total_w = 0.910
    card_w = (card_total_w - 3 * card_gap) / 4.0
    loaded_slots_text = add_metric_card(
        figure,
        card_x,
        card_y,
        card_w,
        card_h,
        "Loaded Slots",
        str(gif_data["row_count"]),
    )
    slot_metric_text = add_metric_card(
        figure,
        card_x + (card_w + card_gap),
        card_y,
        card_w,
        card_h,
        "Displayed Slot",
        "n/a",
    )
    min_metric_text = add_metric_card(
        figure,
        card_x + 2 * (card_w + card_gap),
        card_y,
        card_w,
        card_h,
        "Minimum",
        "n/a",
    )
    max_metric_text = add_metric_card(
        figure,
        card_x + 3 * (card_w + card_gap),
        card_y,
        card_w,
        card_h,
        "Maximum",
        "n/a",
    )

    add_figure_panel(figure, 0.045, 0.115, 0.665, 0.665, facecolor="#ffffff")
    add_figure_panel(figure, 0.735, 0.115, 0.220, 0.665, facecolor="#f7f9fc")

    map_axis = figure.add_axes([0.065, 0.250, 0.625, 0.485], zorder=2)
    map_axis.set_aspect("equal", adjustable="box")
    map_axis.set_xlim(bounds["minX"], bounds["maxX"])
    map_axis.set_ylim(bounds["minY"], bounds["maxY"])
    map_axis.set_xticks([])
    map_axis.set_yticks([])
    map_axis.set_facecolor("#fbfcfe")
    for spine in map_axis.spines.values():
        spine.set_edgecolor("#d8dee7")
        spine.set_linewidth(0.8)

    collection = add_tmap_poly_collection(args, gif_data, np, plt, map_axis, Normalize)
    collection.set_cmap(cmap)
    collection.set_norm(norm)

    colorbar_axis = figure.add_axes([0.065, 0.165, 0.625, 0.030], zorder=2)
    gradient = np.linspace(gif_data["vmin"], gif_data["vmax"], 512, dtype=np.float64).reshape(1, -1)
    colorbar_axis.imshow(
        gradient,
        aspect="auto",
        cmap=cmap,
        norm=norm,
        extent=(gif_data["vmin"], gif_data["vmax"], 0.0, 1.0),
    )
    colorbar_axis.set_yticks([])
    figure.text(
        0.065,
        0.209,
        "Temperature (K)",
        color="#17202a",
        fontsize=10,
        fontweight="semibold",
        ha="left",
        va="bottom",
        zorder=4,
    )
    ticks = colorbar_tick_values(gif_data["vmin"], gif_data["vmax"])
    colorbar_axis.set_xticks(ticks)
    colorbar_axis.set_xticklabels([f"{tick:.4g} K" for tick in ticks], fontsize=8.5)
    colorbar_axis.tick_params(axis="x", length=0, pad=3, colors="#5c6773")
    for spine in colorbar_axis.spines.values():
        spine.set_edgecolor("#d8dee7")
        spine.set_linewidth(0.8)

    side_axis = figure.add_axes([0.735, 0.115, 0.220, 0.665], zorder=2)
    side_axis.set_xlim(0.0, 1.0)
    side_axis.set_ylim(0.0, 1.0)
    side_axis.axis("off")

    side_axis.add_patch(
        Rectangle((0.065, 0.905), 0.235, 0.058, facecolor="#ffffff", edgecolor="#9aa6b2", linewidth=0.8)
    )
    side_axis.text(0.182, 0.934, "Play", color="#17202a", fontsize=8.5, ha="center", va="center")
    side_axis.add_patch(
        Rectangle((0.065, 0.835), 0.870, 0.030, facecolor="#d8dee7", edgecolor="#c9d2dc", linewidth=0.5)
    )
    progress_fill = Rectangle((0.065, 0.835), 0.0, 0.030, facecolor="#3478f6", edgecolor="none")
    side_axis.add_patch(progress_fill)
    slot_text = side_axis.text(
        0.065,
        0.790,
        "",
        color="#5c6773",
        fontsize=8.8,
        ha="left",
        va="top",
        wrap=True,
    )

    side_axis.add_patch(
        Rectangle((0.065, 0.600), 0.870, 0.145, facecolor="#ffffff", edgecolor="#d8dee7", linewidth=0.8)
    )
    selected_text = side_axis.text(
        0.090,
        0.720,
        "",
        color="#17202a",
        fontsize=8.8,
        ha="left",
        va="top",
        linespacing=1.35,
        wrap=True,
    )
    side_axis.text(
        0.065,
        0.535,
        "Hottest cells",
        color="#17202a",
        fontsize=10.0,
        fontweight="semibold",
        ha="left",
        va="top",
    )
    hotspot_texts = [
        side_axis.text(
            0.075,
            0.490 - index * 0.060,
            "",
            color="#17202a",
            fontsize=8.8,
            ha="left",
            va="top",
        )
        for index in range(6)
    ]

    def update(frame_row):
        temperatures = frame_row["values"]
        min_temp = float(temperatures.min())
        max_temp = float(temperatures.max())
        collection.set_array(temperatures)

        display_slot = frame_row["slot"] + 1
        slot_metric_text.set_text(str(display_slot))
        min_metric_text.set_text(format_gif_temp(min_temp))
        max_metric_text.set_text(format_gif_temp(max_temp))
        progress = 1.0 if gif_data["row_count"] <= 1 else frame_row["slot"] / (gif_data["row_count"] - 1)
        progress_fill.set_width(0.870 * progress)
        slot_text.set_text(f"Slot {display_slot} / {gif_data['row_count']} | cells {geometry['cell_count']}")

        hottest_indices = np.argsort(temperatures)[::-1][:6]
        hottest_index = int(hottest_indices[0])
        x, y, width, height = cells[hottest_index]
        selected_text.set_text(
            f"Cell {hottest_index + 1} / {geometry['cell_count']}\n"
            f"Temperature: {format_gif_temp(float(temperatures[hottest_index]))}\n"
            f"Position: {x:.3g}, {y:.3g}\n"
            f"Size: {width:.3g} x {height:.3g}"
        )
        for rank, text in enumerate(hotspot_texts):
            if rank >= len(hottest_indices):
                text.set_text("")
                continue
            index = int(hottest_indices[rank])
            cell_x, cell_y, _, _ = cells[index]
            text.set_text(
                f"{rank + 1}. Cell {index + 1}: {format_gif_temp(float(temperatures[index]))} "
                f"({cell_x:.3g}, {cell_y:.3g})"
            )

        return (
            collection,
            loaded_slots_text,
            slot_metric_text,
            min_metric_text,
            max_metric_text,
            progress_fill,
            slot_text,
            selected_text,
            *hotspot_texts,
        )

    update(gif_data["frame_rows"][0])
    animation = FuncAnimation(
        figure,
        update,
        frames=gif_data["frame_rows"],
        interval=1000.0 / args.gif_fps,
        blit=False,
        repeat=True,
        cache_frame_data=False,
    )
    return save_gif_with_fallback(
        args,
        plt,
        figure,
        animation,
        gif_path,
        ImageMagickWriter,
        PillowWriter,
    )


def render_tmap_gif_once(args, coords_path, map_path, gif_path):
    validate_gif_args(args)

    modules = load_gif_modules()
    np = modules[0]
    gif_data = prepare_tmap_gif_data(args, coords_path, map_path, np)

    if args.gif_layout == "map":
        writer_name, width_px, height_px = render_tmap_gif_map_layout(args, gif_data, modules, gif_path)
    else:
        writer_name, width_px, height_px = render_tmap_gif_interface_layout(
            args,
            coords_path,
            map_path,
            gif_data,
            modules,
            gif_path,
        )

    if not args.quiet:
        print(
            f"Wrote {gif_path} with {len(gif_data['frame_rows'])} frame(s) from {gif_data['row_count']} row(s) "
            f"and {gif_data['geometry']['cell_count']} cell(s) at {width_px}x{height_px}px, "
            f"{args.gif_fps:g} fps using {writer_name} ({args.gif_layout} layout)",
            flush=True,
        )

    return len(gif_data["frame_rows"])

def load_optional_floorplan_regions(coords_path):
    floorplan_path = coords_path.parent / "floorplan_nopower.flp"

    if not floorplan_path.exists():
        return []

    return load_floorplan_regions(floorplan_path)


def build_tmap_payload(
    args,
    coords_path,
    map_path,
    html_path,
    geometry,
    slots,
    row_count,
    data_min,
    data_max,
    live_version=None,
    run_token=None,
):
    floorplan_regions = load_optional_floorplan_regions(coords_path)

    if data_min is None or data_max is None:
        data_min = args.vmin if args.vmin is not None else 300.0
        data_max = args.vmax if args.vmax is not None else data_min + 1.0

    vmin = args.vmin if args.vmin is not None else data_min
    vmax = args.vmax if args.vmax is not None else data_max

    if vmax <= vmin:
        vmax = vmin + 1.0

    latest_slot = slots[-1] if slots else None
    slot_index = latest_slot["slot"] if latest_slot else -1
    payload = {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "coordsPath": str(coords_path),
        "mapPath": str(map_path),
        "htmlPath": str(html_path),
        "cells": geometry["cells"],
        "floorplanRegions": floorplan_regions,
        "bounds": geometry["bounds"],
        "slots": slots,
        "slotIndex": slot_index,
        "rowCount": row_count,
        "vmin": vmin,
        "vmax": vmax,
        "dataMin": data_min,
        "dataMax": data_max,
        "cmap": args.cmap,
        "turboStops": TURBO_STOPS,
    }

    if live_version is not None:
        payload["liveVersion"] = live_version

    if run_token is not None:
        payload["runToken"] = run_token

    return payload


def tmap_initial_metrics(payload):
    slots = payload["slots"]
    latest_slot = slots[-1] if slots else None
    slot_index = latest_slot["slot"] if latest_slot else -1
    latest_values = latest_slot["values"] if latest_slot else []
    latest_min = min(latest_values) if latest_values else None
    latest_max = max(latest_values) if latest_values else None

    return {
        "rowCount": str(payload["rowCount"]),
        "slotMetric": str(slot_index + 1) if latest_slot else "n/a",
        "minMetric": f"{latest_min:.1f} K" if latest_min is not None else "n/a",
        "maxMetric": f"{latest_max:.1f} K" if latest_max is not None else "n/a",
        "slotStatus": (
            f"Slot {slot_index + 1} of {payload['rowCount']} ({len(slots)}/{len(slots)} loaded), following latest"
            if latest_slot
            else "No complete Tmap slots loaded yet."
        ),
    }


def tmap_sidecar_paths(html_path):
    return (
        html_path.with_name(f"{html_path.stem}.manifest.js"),
        html_path.with_name(f"{html_path.stem}.payload.js"),
    )


def javascript_assignment(name, value):
    return f"window.{name} = {json.dumps(value, separators=(',', ':'))};\n"


def write_tmap_sidecars(manifest_path, payload_path, payload):
    manifest = {
        "version": payload.get("liveVersion", payload["rowCount"]),
        "rowCount": payload["rowCount"],
        "slotIndex": payload["slotIndex"],
        "runToken": payload.get("runToken", ""),
        "payloadScript": payload_path.name,
        "generatedAt": payload["generatedAt"],
    }

    write_text_atomic(payload_path, javascript_assignment("__tmapPayload", payload))
    write_text_atomic(manifest_path, javascript_assignment("__tmapManifest", manifest))


def build_tmap_html_dashboard(
    args,
    coords_path,
    map_path,
    html_path,
    geometry,
    slots,
    row_count,
    data_min,
    data_max,
    refresh_seconds,
    live_config=None,
    payload=None,
):
    if payload is None:
        payload = build_tmap_payload(
            args,
            coords_path,
            map_path,
            html_path,
            geometry,
            slots,
            row_count,
            data_min,
            data_max,
        )

    initial_metrics = tmap_initial_metrics(payload)
    payload_json = json.dumps(payload, separators=(",", ":"))
    live_config_json = "null" if live_config is None else json.dumps(live_config, separators=(",", ":"))
    refresh_tag = ""

    if live_config is None and refresh_seconds and refresh_seconds > 0.0:
        refresh_tag = f'<meta http-equiv="refresh" content="{html.escape(html_number(refresh_seconds))}">'

    template = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
__REFRESH_TAG__
<title>3D-ICE Tmap</title>
<style>
:root { color-scheme: light; --ink: #17202a; --muted: #5c6773; --line: #d8dee7; --panel: #f7f9fc; }
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: #ffffff; }
main { max-width: 1280px; margin: 0 auto; padding: 20px; }
h1 { margin: 0 0 6px; font-size: 22px; font-weight: 650; }
.meta { color: var(--muted); font-size: 13px; line-height: 1.5; overflow-wrap: anywhere; }
.summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 14px 0; }
.metric { border: 1px solid var(--line); border-radius: 6px; padding: 10px; background: var(--panel); }
.metric strong { display: block; font-size: 20px; margin-top: 4px; }
.map-wrap { border: 1px solid var(--line); border-radius: 6px; padding: 10px; background: #fff; min-width: 0; }
.slot-controls { display: grid; gap: 8px; margin: 0 0 10px; padding: 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); }
.slot-control-row { display: grid; grid-template-columns: auto minmax(0, 1fr) auto auto; gap: 8px; align-items: center; }
button { border: 1px solid #9aa6b2; border-radius: 5px; background: #fff; padding: 7px 10px; cursor: pointer; white-space: nowrap; }
button:hover:not(:disabled) { background: #eef3f8; }
button:disabled { opacity: 0.52; cursor: not-allowed; }
#slotSlider { width: 100%; margin: 0; }
.slot-status { color: var(--muted); font-size: 14px; line-height: 1.4; overflow-wrap: anywhere; }
canvas { width: 100%; height: auto; display: block; background: #fbfcfe; }
.map-legend { margin-top: 10px; }
.legend-title { margin-bottom: 6px; color: var(--ink); font-size: 14px; font-weight: 650; }
.legend-bar { height: 16px; border-radius: 3px; background: __TURBO_GRADIENT__; border: 1px solid var(--line); }
.legend-labels { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 4px; margin-top: 5px; font-size: 13px; color: var(--muted); }
.legend-labels span { text-align: center; white-space: nowrap; }
.legend-labels span:first-child { text-align: left; }
.legend-labels span:last-child { text-align: right; }
#tooltip { margin-top: 10px; min-height: 42px; font-size: 15px; line-height: 1.45; color: var(--muted); overflow-wrap: anywhere; }
@media (max-width: 860px) { .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 680px) { .slot-control-row { grid-template-columns: repeat(3, minmax(0, 1fr)); } #slotSlider { grid-column: 1 / -1; grid-row: 1; } }
</style>
</head>
<body>
<main>
<header>
  <h1>3D-ICE Tmap</h1>
  <div class="meta" id="generatedMeta">Generated __GENERATED_AT__ from <code>__MAP_PATH__</code></div>
</header>
<section class="summary">
  <div class="metric">Loaded Slots<strong id="rowCount">__INITIAL_ROW_COUNT__</strong></div>
  <div class="metric">Displayed Slot<strong id="slotMetric">__INITIAL_SLOT_METRIC__</strong></div>
  <div class="metric">Minimum<strong id="minMetric">__INITIAL_MIN_METRIC__</strong></div>
  <div class="metric">Maximum<strong id="maxMetric">__INITIAL_MAX_METRIC__</strong></div>
</section>
<section class="map-wrap">
  <div class="slot-controls" aria-label="Tmap slot controls">
    <div class="slot-control-row">
      <button id="prevSlot" type="button">Previous</button>
      <input id="slotSlider" type="range" min="1" max="1" value="1" step="1" aria-label="Displayed slot">
      <button id="nextSlot" type="button">Next</button>
      <button id="latestSlot" type="button">Latest</button>
    </div>
    <div class="slot-status" id="slotStatus">__INITIAL_SLOT_STATUS__</div>
  </div>
  <canvas id="mapCanvas"></canvas>
  __COLORBAR_HTML__
  <div id="tooltip">Move over the map to inspect cell coordinates and temperature.</div>
</section>
</main>
<script id="temperature-data" type="application/json">__PAYLOAD_JSON__</script>
<script>
let data = JSON.parse(document.getElementById('temperature-data').textContent);
let slots = Array.isArray(data.slots) ? data.slots : [];
const liveConfig = __LIVE_CONFIG_JSON__;
const canvas = document.getElementById('mapCanvas');
const ctx = canvas.getContext('2d');
const generatedMeta = document.getElementById('generatedMeta');
const rowCount = document.getElementById('rowCount');
const slotMetric = document.getElementById('slotMetric');
const minMetric = document.getElementById('minMetric');
const maxMetric = document.getElementById('maxMetric');
const tooltip = document.getElementById('tooltip');
const slotSlider = document.getElementById('slotSlider');
const prevSlot = document.getElementById('prevSlot');
const nextSlot = document.getElementById('nextSlot');
const latestSlot = document.getElementById('latestSlot');
const slotStatus = document.getElementById('slotStatus');
const legendLabels = document.querySelector('.legend-labels');
const storageKey = `3dice-tmap-html:${data.mapPath}`;
let cssWidth = 0;
let cssHeight = 0;
let selectedIndex = slots.length ? slots.length - 1 : -1;
let followingLatest = true;
let liveVersion = Number.isFinite(data.liveVersion) ? data.liveVersion : 0;
let liveRunToken = data.runToken || '';
let livePollInFlight = false;

function fmt(value, digits = 1) {
  if (!Number.isFinite(value)) return 'n/a';
  return value.toFixed(digits);
}

function clampIndex(index) {
  if (!slots.length) return -1;
  return Math.max(0, Math.min(slots.length - 1, index));
}

function slotNumberAt(index) {
  if (index < 0 || index >= slots.length) return null;
  return slots[index].slot + 1;
}

function indexForSlotNumber(slotNumber) {
  if (!slots.length || !Number.isFinite(slotNumber)) return -1;
  const found = slots.findIndex(slot => slot.slot + 1 === slotNumber);
  if (found >= 0) return found;
  return clampIndex(slotNumber - 1);
}

function parseViewerState(value) {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value);
    if (parsed && parsed.mode === 'slot' && Number.isFinite(parsed.slotNumber)) {
      return { mode: 'slot', slotNumber: parsed.slotNumber };
    }
    if (parsed && parsed.mode === 'latest') return { mode: 'latest' };
  } catch (_) {
    return null;
  }
  return null;
}

function readHashState() {
  const raw = window.location.hash.replace(/^#/, '');
  if (!raw) return null;
  const params = new URLSearchParams(raw);
  const mode = params.get('mode');
  const slotNumber = Number.parseInt(params.get('slot'), 10);
  if (mode === 'slot' && Number.isFinite(slotNumber)) return { mode: 'slot', slotNumber };
  if (mode === 'latest') return { mode: 'latest' };
  return null;
}

function readStoredState() {
  try {
    return parseViewerState(window.localStorage.getItem(storageKey));
  } catch (_) {
    return null;
  }
}

function saveViewerState() {
  const slotNumber = slotNumberAt(selectedIndex);
  const state = followingLatest ? { mode: 'latest' } : { mode: 'slot', slotNumber };
  try {
    window.localStorage.setItem(storageKey, JSON.stringify(state));
  } catch (_) {}
  try {
    const params = new URLSearchParams();
    params.set('mode', state.mode);
    if (state.mode === 'slot' && slotNumber !== null) params.set('slot', String(slotNumber));
    window.history.replaceState(null, '', `#${params.toString()}`);
  } catch (_) {}
}

function applyInitialState() {
  const state = readHashState() || readStoredState();
  if (state && state.mode === 'slot') {
    selectedIndex = indexForSlotNumber(state.slotNumber);
    followingLatest = false;
  } else {
    selectedIndex = slots.length ? slots.length - 1 : -1;
    followingLatest = true;
  }
}

function currentSlot() {
  if (selectedIndex < 0 || selectedIndex >= slots.length) return null;
  return slots[selectedIndex];
}

function currentValues() {
  const slot = currentSlot();
  return slot ? slot.values : [];
}

function currentMinMax(values) {
  if (!values.length) return [NaN, NaN];
  let minValue = values[0];
  let maxValue = values[0];
  for (const value of values) {
    if (value < minValue) minValue = value;
    if (value > maxValue) maxValue = value;
  }
  return [minValue, maxValue];
}

function colorbarTickValues(vmin, vmax, count = 7) {
  if (count <= 1) return [vmin];
  const values = [];
  for (let index = 0; index < count; index += 1) {
    values.push(vmin + (vmax - vmin) * index / (count - 1));
  }
  return values;
}

function updateLegendLabels() {
  if (!legendLabels) return;
  legendLabels.innerHTML = colorbarTickValues(data.vmin, data.vmax)
    .map(value => `<span>${fmt(value)} K</span>`)
    .join('');
}

function updateGeneratedMeta() {
  if (!generatedMeta) return;
  generatedMeta.textContent = `Generated ${data.generatedAt} from ${data.mapPath}`;
}

function colorFor(value) {
  const t = Math.max(0, Math.min(1, (value - data.vmin) / (data.vmax - data.vmin)));
  const stops = data.turboStops;
  const scaled = t * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(scaled));
  const f = scaled - i;
  const a = stops[i];
  const b = stops[i + 1];
  const r = Math.round(a[0] + (b[0] - a[0]) * f);
  const g = Math.round(a[1] + (b[1] - a[1]) * f);
  const bl = Math.round(a[2] + (b[2] - a[2]) * f);
  return `rgb(${r},${g},${bl})`;
}

function mapX(x) {
  return (x - data.bounds.minX) * cssWidth / data.bounds.width;
}

function mapY(y, height) {
  return cssHeight - ((y + height - data.bounds.minY) * cssHeight / data.bounds.height);
}

function drawFloorplanOutlines() {
  if (!data.floorplanRegions || !data.floorplanRegions.length) return;
  ctx.save();
  ctx.strokeStyle = 'rgba(17, 24, 39, 0.82)';
  ctx.lineWidth = 1.25;
  data.floorplanRegions.forEach(region => {
    const x = mapX(region.x);
    const y = mapY(region.y, region.height);
    const w = region.width * cssWidth / data.bounds.width;
    const h = region.height * cssHeight / data.bounds.height;
    ctx.strokeRect(x, y, w, h);
  });
  ctx.restore();
}

function drawMap() {
  ctx.clearRect(0, 0, cssWidth, cssHeight);
  const values = currentValues();
  if (!values.length) return;
  for (let index = 0; index < data.cells.length && index < values.length; index += 1) {
    const cell = data.cells[index];
    const x = mapX(cell[0]);
    const y = mapY(cell[1], cell[3]);
    const w = Math.max(0.5, cell[2] * cssWidth / data.bounds.width);
    const h = Math.max(0.5, cell[3] * cssHeight / data.bounds.height);
    ctx.fillStyle = colorFor(values[index]);
    ctx.fillRect(x - 0.5, y - 0.5, w + 1.0, h + 1.0);
  }
  drawFloorplanOutlines();
}

function resizeCanvas() {
  const dpr = window.devicePixelRatio || 1;
  cssWidth = Math.max(320, canvas.parentElement.clientWidth - 20);
  cssHeight = Math.max(240, cssWidth * data.bounds.height / data.bounds.width);
  canvas.style.width = `${cssWidth}px`;
  canvas.style.height = `${cssHeight}px`;
  canvas.width = Math.round(cssWidth * dpr);
  canvas.height = Math.round(cssHeight * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  drawMap();
}

function setDefaultTooltip() {
  tooltip.textContent = 'Move over the map to inspect cell coordinates and temperature.';
}

function updateMetrics() {
  const slot = currentSlot();
  const values = currentValues();
  const [slotMin, slotMax] = currentMinMax(values);
  rowCount.textContent = String(data.rowCount);
  slotMetric.textContent = slot ? String(slot.slot + 1) : 'n/a';
  minMetric.textContent = `${fmt(slotMin)} K`;
  maxMetric.textContent = `${fmt(slotMax)} K`;

  slotSlider.disabled = slots.length === 0;
  prevSlot.disabled = selectedIndex <= 0;
  nextSlot.disabled = selectedIndex < 0 || selectedIndex >= slots.length - 1;
  latestSlot.disabled = slots.length === 0 || (followingLatest && selectedIndex === slots.length - 1);
  slotSlider.max = String(Math.max(1, slots.length));
  slotSlider.value = String(Math.max(1, selectedIndex + 1));

  if (!slot) {
    slotStatus.textContent = 'No complete Tmap slots loaded yet.';
    return;
  }

  const modeText = followingLatest ? 'following latest' : 'manual selection';
  slotStatus.textContent = `Slot ${slot.slot + 1} of ${data.rowCount} (${selectedIndex + 1}/${slots.length} loaded), ${modeText}`;
}

function updateView() {
  updateMetrics();
  setDefaultTooltip();
  drawMap();
}

function applyPayload(nextData) {
  if (!nextData || !Array.isArray(nextData.slots) || nextData.slots.length === 0) return;
  const previousSlotNumber = slotNumberAt(selectedIndex);
  data = nextData;
  slots = Array.isArray(data.slots) ? data.slots : [];
  liveVersion = Number.isFinite(data.liveVersion) ? data.liveVersion : liveVersion;
  liveRunToken = data.runToken || liveRunToken;

  if (followingLatest) {
    selectedIndex = slots.length ? slots.length - 1 : -1;
  } else {
    selectedIndex = indexForSlotNumber(previousSlotNumber);
  }

  updateGeneratedMeta();
  updateLegendLabels();
  updateView();
}

function sidecarUrl(path, version) {
  const separator = path.includes('?') ? '&' : '?';
  return `${path}${separator}v=${encodeURIComponent(version)}&t=${Date.now()}`;
}

function loadSidecarScript(path, version, onload, onerror) {
  const script = document.createElement('script');
  script.async = true;
  script.src = sidecarUrl(path, version);
  script.onload = () => {
    script.remove();
    onload();
  };
  script.onerror = () => {
    script.remove();
    if (onerror) onerror();
  };
  document.head.appendChild(script);
}

function payloadIsNewer(payload) {
  const payloadVersion = Number.isFinite(payload.liveVersion) ? payload.liveVersion : 0;
  const payloadRunToken = payload.runToken || '';
  if (payloadRunToken !== liveRunToken) return true;
  return payloadVersion !== liveVersion;
}

function pollLiveManifest() {
  if (!liveConfig || !liveConfig.manifestScript) return;
  if (livePollInFlight) return;
  livePollInFlight = true;
  const finishPoll = () => {
    livePollInFlight = false;
  };
  loadSidecarScript(liveConfig.manifestScript, liveVersion, () => {
    const manifest = window.__tmapManifest;
    if (!manifest) {
      finishPoll();
      return;
    }
    const manifestVersion = Number.isFinite(manifest.version) ? manifest.version : 0;
    const manifestRunToken = manifest.runToken || '';
    if (manifestVersion === liveVersion && manifestRunToken === liveRunToken) {
      finishPoll();
      return;
    }
    const payloadScript = manifest.payloadScript || liveConfig.payloadScript;
    if (!payloadScript) {
      finishPoll();
      return;
    }
    loadSidecarScript(payloadScript, manifestVersion, () => {
      const payload = window.__tmapPayload;
      if (payload && payloadIsNewer(payload)) applyPayload(payload);
      finishPoll();
    }, finishPoll);
  }, finishPoll);
}

function startLivePolling() {
  if (!liveConfig || !liveConfig.manifestScript) return;
  const refreshMs = Math.max(250, Number(liveConfig.refreshMs) || 1000);
  window.setTimeout(pollLiveManifest, 100);
  window.setInterval(pollLiveManifest, refreshMs);
}

function setSelectedIndex(index, followLatest, persist) {
  selectedIndex = clampIndex(index);
  followingLatest = followLatest;
  if (persist) saveViewerState();
  updateView();
}

function inspectCell(event) {
  const values = currentValues();
  if (!values.length) return;
  const rect = canvas.getBoundingClientRect();
  const px = (event.clientX - rect.left) * cssWidth / rect.width;
  const py = (event.clientY - rect.top) * cssHeight / rect.height;
  const physicalX = data.bounds.minX + px * data.bounds.width / cssWidth;
  const physicalY = data.bounds.minY + (cssHeight - py) * data.bounds.height / cssHeight;
  let bestIndex = -1;
  for (let index = 0; index < data.cells.length; index += 1) {
    const cell = data.cells[index];
    if (physicalX >= cell[0] && physicalX <= cell[0] + cell[2] && physicalY >= cell[1] && physicalY <= cell[1] + cell[3]) {
      bestIndex = index;
      break;
    }
  }
  if (bestIndex < 0) return;
  const cell = data.cells[bestIndex];
  tooltip.innerHTML = `Cell ${bestIndex + 1} / ${data.cells.length} | Temperature: <strong>${fmt(values[bestIndex])} K</strong><br>Position: ${fmt(cell[0], 1)}, ${fmt(cell[1], 1)} | Size: ${fmt(cell[2], 1)} x ${fmt(cell[3], 1)}`;
}

applyInitialState();
updateMetrics();
updateLegendLabels();
resizeCanvas();
slotSlider.addEventListener('input', () => setSelectedIndex(Number.parseInt(slotSlider.value, 10) - 1, false, true));
prevSlot.addEventListener('click', () => setSelectedIndex(selectedIndex - 1, false, true));
nextSlot.addEventListener('click', () => setSelectedIndex(selectedIndex + 1, false, true));
latestSlot.addEventListener('click', () => setSelectedIndex(slots.length - 1, true, true));
canvas.addEventListener('mousemove', inspectCell);
window.addEventListener('resize', resizeCanvas);
startLivePolling();
</script>
</body>
</html>
"""
    return (
        template.replace("__REFRESH_TAG__", refresh_tag)
        .replace("__TURBO_GRADIENT__", turbo_gradient_css())
        .replace("__GENERATED_AT__", html.escape(payload["generatedAt"]))
        .replace("__MAP_PATH__", html.escape(str(map_path)))
        .replace("__COLORBAR_HTML__", colorbar_html(payload["vmin"], payload["vmax"]))
        .replace("__INITIAL_ROW_COUNT__", html.escape(initial_metrics["rowCount"]))
        .replace("__INITIAL_SLOT_METRIC__", html.escape(initial_metrics["slotMetric"]))
        .replace("__INITIAL_MIN_METRIC__", html.escape(initial_metrics["minMetric"]))
        .replace("__INITIAL_MAX_METRIC__", html.escape(initial_metrics["maxMetric"]))
        .replace("__INITIAL_SLOT_STATUS__", html.escape(initial_metrics["slotStatus"]))
        .replace("__LIVE_CONFIG_JSON__", live_config_json)
        .replace("__PAYLOAD_JSON__", payload_json)
    )


def render_tmap_html_once(args, coords_path, map_path, html_path, refresh_seconds=0.0, allow_empty=False):
    geometry = load_tmap_geometry_cells(coords_path)

    if map_path.exists():
        slots, row_count, data_min, data_max = read_existing_tmap_slots(
            map_path,
            geometry["cell_count"],
            follow=allow_empty,
        )
    else:
        slots, row_count, data_min, data_max = [], 0, None, None

    if not slots:
        if not allow_empty:
            raise ValueError(f"{map_path}: no complete Tmap rows found")
        if not args.quiet:
            if html_path.exists():
                print(f"No complete Tmap slots found in {map_path}; keeping existing {html_path}", flush=True)
            else:
                print(f"Waiting for complete Tmap slots in {map_path} before writing {html_path} ...", flush=True)
        return 0

    dashboard = build_tmap_html_dashboard(
        args,
        coords_path,
        map_path,
        html_path,
        geometry,
        slots,
        row_count,
        data_min,
        data_max,
        refresh_seconds,
    )
    write_text_atomic(html_path, dashboard)

    if not args.quiet:
        latest_slot = slots[-1]["slot"] + 1 if slots else "n/a"
        print(f"Wrote {html_path} with {len(slots)} Tmap slot(s), latest slot {latest_slot}", flush=True)

    return row_count


def file_signature(stat_result):
    return (stat_result.st_dev, stat_result.st_ino, stat_result.st_size, stat_result.st_mtime_ns)


def file_identity(stat_result):
    return (stat_result.st_dev, stat_result.st_ino)


def print_live_tmap_message(args, state, key, message):
    if args.quiet:
        return

    if state.get(key) == message:
        return

    print(message, flush=True)
    state[key] = message


def reset_tmap_follow_state(state, map_identity=None):
    state["geometry"] = None
    state["last_rendered_row_count"] = 0
    state["last_combined_signature"] = None
    state["map_identity"] = map_identity
    state["map_size"] = None
    state["mismatch_key"] = None
    state["mismatch_count"] = 0
    state["html_shell_written"] = False
    state["live_version"] = 0
    state["run_token"] = ""


def wait_for_live_tmap_paths(args, coords_path, map_path, state):
    try:
        coords_stat = coords_path.stat()
    except FileNotFoundError:
        print_live_tmap_message(args, state, "wait_coords", f"Waiting for {coords_path} ...")
        return None, None

    try:
        map_stat = map_path.stat()
    except FileNotFoundError:
        print_live_tmap_message(args, state, "wait_map", f"Waiting for {map_path} ...")
        return coords_stat, None

    return coords_stat, map_stat


def load_validated_tmap_geometry_for_follow(args, coords_path, map_path, state, coords_signature):
    first_width, first_line_number = read_first_tmap_row_width(map_path, follow=True)

    if first_width is None:
        print_live_tmap_message(
            args,
            state,
            "wait_tmap_row",
            f"Waiting for a complete Tmap row in {map_path} ...",
        )
        return None

    geometry = state.get("geometry")
    if geometry is not None and geometry["cell_count"] == first_width:
        state["mismatch_key"] = None
        state["mismatch_count"] = 0
        return geometry

    if geometry is not None and geometry["cell_count"] != first_width:
        geometry = None
        state["geometry"] = None
        state["last_rendered_row_count"] = 0

    geometry = load_tmap_geometry_cells(coords_path, follow=True, allow_empty=True)

    if geometry is None:
        print_live_tmap_message(
            args,
            state,
            "wait_coords_rows",
            f"Waiting for complete coordinate rows in {coords_path} ...",
        )
        return None

    if geometry["cell_count"] != first_width:
        mismatch_key = (coords_signature, first_width, first_line_number, geometry["cell_count"])
        if state.get("mismatch_key") == mismatch_key:
            state["mismatch_count"] = state.get("mismatch_count", 0) + 1
        else:
            state["mismatch_key"] = mismatch_key
            state["mismatch_count"] = 1

        if state["mismatch_count"] >= STABLE_TMAP_GEOMETRY_MISMATCH_POLLS:
            raise ValueError(
                f"{coords_path}: complete coordinate count {geometry['cell_count']} does not match "
                f"{map_path}:{first_line_number} Tmap width {first_width}"
            )

        print_live_tmap_message(
            args,
            state,
            "wait_geometry_match",
            f"Waiting for {coords_path} to match {first_width} Tmap cell(s); "
            f"currently has {geometry['cell_count']} complete coordinate row(s).",
        )
        return None

    state["geometry"] = geometry
    state["mismatch_key"] = None
    state["mismatch_count"] = 0
    return geometry


def render_tmap_html_follow_snapshot(args, coords_path, map_path, html_path, state, refresh_seconds, coords_signature):
    geometry = load_validated_tmap_geometry_for_follow(
        args,
        coords_path,
        map_path,
        state,
        coords_signature,
    )

    if geometry is None:
        return False

    slots, row_count, data_min, data_max = read_existing_tmap_slots(
        map_path,
        geometry["cell_count"],
        follow=True,
    )

    if not slots:
        print_live_tmap_message(
            args,
            state,
            "wait_tmap_slots",
            f"Waiting for complete Tmap slots in {map_path} before writing {html_path} ...",
        )
        return False

    if row_count < state["last_rendered_row_count"]:
        state["last_rendered_row_count"] = 0

    if row_count <= state["last_rendered_row_count"]:
        return False

    live_version = state.get("live_version", 0) + 1
    run_token = state.get("run_token", "")
    payload = build_tmap_payload(
        args,
        coords_path,
        map_path,
        html_path,
        geometry,
        slots,
        row_count,
        data_min,
        data_max,
        live_version=live_version,
        run_token=run_token,
    )
    manifest_path, payload_path = tmap_sidecar_paths(html_path)
    write_tmap_sidecars(manifest_path, payload_path, payload)

    if not state.get("html_shell_written") or not html_path.exists():
        live_config = {
            "manifestScript": manifest_path.name,
            "payloadScript": payload_path.name,
            "refreshMs": max(250, int(refresh_seconds * 1000.0)),
        }
        dashboard = build_tmap_html_dashboard(
            args,
            coords_path,
            map_path,
            html_path,
            geometry,
            slots,
            row_count,
            data_min,
            data_max,
            0.0,
            live_config=live_config,
            payload=payload,
        )
        write_text_atomic(html_path, dashboard)
        state["html_shell_written"] = True

    if not args.quiet:
        previous_row_count = state["last_rendered_row_count"]
        added_count = row_count - previous_row_count
        latest_slot = slots[-1]["slot"] + 1
        print(
            f"Updated {manifest_path.name}/{payload_path.name} for {len(slots)} Tmap slot(s), latest slot {latest_slot} "
            f"(added {added_count} complete slot(s))",
            flush=True,
        )

    state["last_rendered_row_count"] = row_count
    state["live_version"] = live_version
    return True


def render_tmap_html_follow(args, coords_path, map_path, html_path):
    state = {}
    reset_tmap_follow_state(state)
    refresh_seconds = args.html_refresh if args.html_refresh is not None else args.poll

    while True:
        coords_stat, map_stat = wait_for_live_tmap_paths(args, coords_path, map_path, state)

        if coords_stat is not None and map_stat is not None:
            current_map_identity = file_identity(map_stat)
            previous_map_identity = state.get("map_identity")
            previous_map_size = state.get("map_size")

            if previous_map_identity != current_map_identity:
                reset_tmap_follow_state(state, map_identity=current_map_identity)
                if previous_map_identity is not None and not args.quiet:
                    print(f"Detected new Tmap file {map_path}; restarting live follow state.", flush=True)
            elif previous_map_size is not None and map_stat.st_size < previous_map_size:
                reset_tmap_follow_state(state, map_identity=current_map_identity)
                if not args.quiet:
                    print(f"Detected truncated Tmap file {map_path}; restarting live follow state.", flush=True)

            state["map_identity"] = current_map_identity
            state["map_size"] = map_stat.st_size
            state["run_token"] = f"{current_map_identity[0]}-{current_map_identity[1]}"

            combined_signature = (file_signature(coords_stat), file_signature(map_stat))
            if combined_signature != state.get("last_combined_signature") or state.get("geometry") is None:
                render_tmap_html_follow_snapshot(
                    args,
                    coords_path,
                    map_path,
                    html_path,
                    state,
                    refresh_seconds,
                    file_signature(coords_stat),
                )
                state["last_combined_signature"] = combined_signature

        time.sleep(args.poll)

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
    coords_path = Path(args.coords)
    map_path = Path(args.map)

    if args.html_refresh is not None and args.html_refresh < 0.0:
        raise ValueError("--html-refresh must be non-negative")

    if args.follow:
        if args.gif is not None:
            raise ValueError("--gif is an offline export; use --once for GIF output")
        render_tmap_html_follow(args, coords_path, map_path, Path(args.html))
        return

    if args.html is not None:
        render_tmap_html_once(args, coords_path, map_path, Path(args.html))

    if args.gif is not None:
        render_tmap_gif_once(args, coords_path, map_path, Path(args.gif))


def run_floorplan_mode(args):
    floorplan_path = Path(args.floorplan)
    tflp_path = Path(args.tflp)

    if args.html_refresh is not None and args.html_refresh < 0.0:
        raise ValueError("--html-refresh must be non-negative")

    if args.follow:
        if args.gif is not None:
            raise ValueError("--gif is an offline export; use --once for GIF output")
        html_path = Path(args.html)
        render_html_follow(args, floorplan_path, tflp_path, html_path)
        return

    if args.html is not None:
        render_html_once(args, floorplan_path, tflp_path, Path(args.html))

    if args.gif is not None:
        render_gif_once(args, floorplan_path, tflp_path, Path(args.gif))


def main():
    args = parse_args()

    if args.poll <= 0.0:
        raise ValueError("--poll must be positive")

    mode = validate_mode_args(args)

    if not args.follow and not args.once:
        if args.gif is not None:
            args.once = True
        else:
            args.follow = True

    if mode == "floorplan":
        run_floorplan_mode(args)
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
