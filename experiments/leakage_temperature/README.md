# Leakage–temperature feedback experiment

This experiment compares two full default-application co-simulations while
holding the 25 °C component leakage references fixed:

1. `constant`: live temperatures are exchanged, but leakage stays at its 25 °C
   reference;
2. `temperature_aware`: each returned temperature re-interpolates an
   exponential-like leakage table before the next power interval.

This pairing isolates the effect of temperature sensitivity.

Run and analyze the complete experiment with:

```bash
experiments/leakage_temperature/run_experiment.sh
```

The script bootstraps and builds the control once, then reuses that exact
simulator/workload build for the treatment; only the runtime power profile and
run directory change.

The result directory contains:

- `REPORT.md`: academic-style method, results, interpretation, and limitations;
- `metrics.csv` and `comparison.json`: slide-ready numbers;
- `timeseries.csv`: tidy per-component power and temperature data;
- six figures in 320 dpi PNG and vector PDF formats, including technology-node
  scaling assumptions and dual-axis temperature/leakage histories for the
  temperature-aware profile;
- `leakage_model_tables.csv`: every generated lookup point for the default
  design across 22, 12, 7, and 5 nm.

Analyze already completed runs without repeating simulation:

```bash
MPLCONFIGDIR=/tmp/softhier-leakage-mpl \
  conda run -n py312 python experiments/leakage_temperature/analyze.py \
  --constant-run runs/leakage_constant/latest \
  --temperature-aware-run runs/leakage_temperature_aware/latest \
  --output-dir experiments/leakage_temperature/results/manual
```

The coefficients and their limitations are documented in
`SoftHier/pulp/pulp/chips/soft_hier_old/power_models/README.md`.
