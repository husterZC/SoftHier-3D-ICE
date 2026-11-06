# Detailed layer-stack figure

`plot_layer_stack_detailed.py` draws a detailed face-to-face package schematic,
including separate bulk silicon, active source, and BEOL layers in each die,
four TIM layers, hybrid-bond copper pads, package bumps, four substrate layers,
die-thickness brackets, cooling arrows, and face-to-face orientation arrows.

The script is self-contained. Copy this single file to any folder or machine;
only Python and Matplotlib are required. It does not read the reference image,
repository configuration, simulation results, or separate fonts.

```bash
python plot_layer_stack_detailed.py --output-dir ./figures
```

Defaults are logic on DRAM, 300 K coolant, and
25,000 W/(m²·K) heat transfer coefficient. These are embedded figure settings,
independent of the repository's current simulation configuration. Both dies
have 49 µm bulk silicon, 1 µm active source, and 5 µm BEOL. Other thicknesses are
50 µm copper, 30 µm TIM, 2 µm hybrid bond, 50 µm package bumps, and
1,000 µm substrate. TIM and substrate labels show their total thicknesses.

Outputs are `layer_stack_detailed_logic_on_dram.pdf`, `.svg`, `.png`, and
`.json`. PDF/SVG preserve vector graphics; PNG defaults to 600 dpi. The JSON
records the plotted settings and is an output, not a required input. The script
uses the DejaVu Sans font bundled with Matplotlib. Matching exports are
overwritten; without `--output-dir`, outputs are saved beside the script.

To reverse the die order or change cooling annotations:

```bash
python plot_layer_stack_detailed.py \
  --stack-order dram_on_logic \
  --coolant-temperature 300 \
  --heat-transfer-coefficient 15000 \
  --output-dir ./figures
```

Physical thickness constants are near the top of the file. `DISPLAY_HEIGHT`
controls the drawing proportions separately, so thin active and bonding layers
remain visible. Layer heights and interconnect counts are schematic, not to
scale.
