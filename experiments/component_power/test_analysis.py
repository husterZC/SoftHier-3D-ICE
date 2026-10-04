"""Small analysis regressions; run with the study's Python/numpy environment."""

import copy
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import analyze
import summarize


class AnalysisTests(unittest.TestCase):
    def test_full_maps_requires_animations_before_reading_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            message = io.StringIO()
            with patch.object(sys, "argv", ["analyze.py", "--manifest", str(folder / "missing.json"),
                                            "--output", str(folder / "output"), "--full-maps"]):
                with redirect_stderr(message), self.assertRaises(SystemExit) as caught:
                    analyze.main()
            self.assertEqual(caught.exception.code, 2)
            self.assertIn("--full-maps requires --animations", message.getvalue())
            self.assertFalse((folder / "output").exists())

    def test_mixed_floorplan_studies_are_rejected_before_reading_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            paths = [folder / "original.json", folder / "square.json"]
            paths[0].write_text(json.dumps(dict(prefix="original", runs=[])))
            paths[1].write_text(json.dumps(dict(prefix="square", floorplan_rule="square_bands", runs=[])))
            with patch.object(sys, "argv", ["analyze.py", "--manifest", *map(str, paths),
                                            "--output", str(folder / "output")]):
                with self.assertRaisesRegex(ValueError, "floorplan_rule"):
                    analyze.main()
            self.assertFalse((folder / "output").exists())

    def test_energy_decomposition(self):
        constant = dict(duration_us=10., leakage_energy_uj=10., total_energy_uj=30.,
            first_leakage_w=1., average_leakage_w=1., average_dynamic_w=2.,
            peak_component_temperature_c=30., peak_cell_temperature_c=31.,
            leakage_growth_during_run_pct=0.)
        aware = dict(constant, leakage_energy_uj=12., total_energy_uj=32.,
            first_leakage_w=1.1, average_leakage_w=1.2, leakage_growth_during_run_pct=20.)
        result = summarize.comparison("test", constant, aware)
        self.assertAlmostEqual(result["initial_reference_offset_pct"], 10.)
        self.assertAlmostEqual(result["subsequent_heating_contribution_pct"], 10.)
        self.assertAlmostEqual(result["leakage_energy_increase_pct"], 20.)
        self.assertAlmostEqual(result["total_energy_increase_pct"], 100. * 2. / 30.)

    def test_sensitivity_boundaries(self):
        profiles = ("constant", "temperature_aware")
        metrics = {("acti", p): {"duration_us": 7.} for p in profiles}
        components = [dict(kernel="acti", profile=p, component=kind,
                           dynamic_energy_uj=2., leakage_energy_uj=1.)
                      for p in profiles for kind in analyze.KINDS]
        baseline = ({}, metrics, components)
        scaled = copy.deepcopy(baseline)
        for row in scaled[2]:
            if row["component"] not in ("memory", "light_redmule"):
                row["dynamic_energy_uj"] *= 2
                row["leakage_energy_uj"] *= 2
        summarize.validate_sensitivity(baseline, scaled, 2.)
        next(r for r in scaled[2] if r["component"] == "memory")["dynamic_energy_uj"] *= 2
        with self.assertRaisesRegex(ValueError, "memory"):
            summarize.validate_sensitivity(baseline, scaled, 2.)

    def test_inventory_coverage_and_disjoint_domains(self):
        def run(paths, traced=None):
            return SimpleNamespace(metadata={"components": [{"simulator_path": p} for p in paths]},
                power_records=[{"components": [dict(path=p, dynamic_w=1., leakage_w=.1, total_w=1.1)
                    for p in (paths if traced is None else traced)]}])
        analyze.validate_inventory(run(["/pe0/scalar_power", "/pe0/ara"]))
        with self.assertRaisesRegex(ValueError, "overlapping"):
            analyze.validate_inventory(run(["/pe0", "/pe0/ara"]))
        with self.assertRaisesRegex(ValueError, "exactly once"):
            analyze.validate_inventory(run(["/pe0/scalar_power", "/pe0/ara"], ["/pe0/ara"]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            analyze.validate_inventory(run(["/pe0/ara", "/pe0/ara"]))


if __name__ == "__main__":
    unittest.main()
