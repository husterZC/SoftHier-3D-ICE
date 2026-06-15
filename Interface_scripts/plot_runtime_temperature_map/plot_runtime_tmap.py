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

"""Live matplotlib viewer for 3D-ICE client-side Tmap files.

Usage:
  python3 plot_runtime_tmap.py --coords COORDS_FILE --map TMAP_FILE

Examples:
  python3 plot_runtime_tmap.py --coords output_top_die_map.coords.txt --map output_top_die_map.txt


Notes:
  - --coords and --map are required.
  - The color scale uses the rendered Temperature map's min/max temperature by default.
  - Use --vmin and/or --vmax to pin the temperature color range across updates.
  - Follow mode is the default and renders only the newest complete Tmap row.


"""

import argparse
import sys
import time
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(
        description="Open a live matplotlib figure for appended 3D-ICE Tmap rows."
    )
    parser.add_argument(
        "--coords",
        required=True,
        help="Path to the Tmap coordinate file, for example output_top_die_map.coords.txt.",
    )
    parser.add_argument(
        "--map",
        required=True,
        help="Path to the Tmap temperature file, for example output_top_die_map.txt.",
    )
    parser.add_argument("--poll", type=float, default=1.0)
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
            "Install dependencies with:\n"
            "  python3 -m pip install -r requirements_tmap_plot.txt\n"
            f"Original import error: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    except Exception as exc:
        print(
            f"ERROR: could not initialize matplotlib backend {backend!r}: {exc}\n"
            "For a local GUI window, install/use a GUI backend such as PyQt5 and run with X11/desktop access.\n"
            "For a non-GUI syntax/render smoke test, use: --backend Agg --once",
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


def main():
    args = parse_args()

    if not args.follow and not args.once:
        args.follow = True

    if args.poll <= 0.0:
        raise ValueError("--poll must be positive")

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
