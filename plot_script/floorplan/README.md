# Logic-die and cluster floorplans

`plot_floorplans.py` is a self-contained script. Copy this single file to another
folder or machine and run it with Python and Matplotlib. All geometry, colors,
and labels are embedded in the script. It needs no repository, SDK,
configuration files, simulation results, or separate fonts.

Install Matplotlib if needed, then run the copied script:

```bash
python -m pip install matplotlib
python plot_floorplans.py
```

By default, outputs are saved beside the script:

- `logic_die_floorplan.pdf`, `.png`, `.svg`: the complete logic die.
- `cluster_floorplan.pdf`, `.png`, `.svg`: one cluster, with component labels
  and numbered callouts for small regions.
- `floorplan_geometry.json`: dimensions, component rectangles, and areas exported
  from the embedded data. This is an output, never an input.

PDF and SVG preserve vector geometry. PNG uses 600 dpi. The script uses DejaVu
Serif, which ships with Matplotlib.

Choose another output directory with `--output-dir` (or `--output`):

```bash
python plot_floorplans.py --output-dir ./figures
```

The embedded layout is a fixed snapshot of the 5 nm DRAM3D `square_bands`
floorplan from 2026-11-06: 4 × 4 clusters, five scalar cores and four Spatz units
per cluster. Each cluster measures 753.561 × 753.561 µm; the logic die measures
3.0142 × 3.0142 mm. All 20 component rectangles per cluster, including `others`,
preserve the coordinates used by the previous source-based plotter.

Edit `CLUSTER_GRID`, `CLUSTER_SHAPE_UM`, and `COMPONENTS` near the top of the
script to change the layout. Lengths are in micrometers. The script checks
bounds, overlaps, and area coverage before plotting. Repository changes do not
affect this fixed snapshot; the drawing remains an area-estimated floorplan.

Use `--dpi 300` to change PNG resolution. Matching output filenames are
overwritten. The former `--arch` and `--floorplan` options have been removed
because this script no longer loads external layout definitions.
