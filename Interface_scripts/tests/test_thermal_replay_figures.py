"""Saved-interval power accounting, FLP ordering, and stack schematic regressions."""

import csv
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from Interface_scripts.tests.test_thermal_replay import contract
from Interface_scripts.geometry_generator.roi2ice_stk import stk_template
from experiments.thermal_replay.activity import digest, write_json
from experiments.thermal_replay.plot_stack import plot_stack, read_stack, schematic_blocks
from experiments.thermal_replay.power_maps import export, floorplan_blocks, plot, read_snapshot, time_ps, validate_pair


class ReplayFigureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="replay-figure-tests-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.recording = self.directory / "recording"
        self.recording.mkdir()
        document = contract()
        document["geometry"]["chip"]["subs"]["logic"]["shape"] = [400., 1000.]
        document["geometry"]["chip"]["subs"]["fixed"] = {
            "type": "comp", "shape": [100., 1000.], "offset": [400., 0.], "subs": {}}
        document["floorplan"]["elements"].append({"name": "chip/fixed", "power": {"constant_w": .04}})
        write_json(self.recording / "system_config.json", document)
        # Floorplan order deliberately differs from component-column order.
        floorplan = """chip__memory :
 position 500, 0;
 dimension 500, 1000;
 discretization 5, 10;
 power values 999, 888;
chip__fixed :
 position 400, 0;
 dimension 100, 1000;
 discretization 1, 10;
chip__logic :
 position 0, 0;
 dimension 400, 1000;
 discretization 4, 10;
"""
        (self.recording / "floorplan.flp").write_text(floorplan)
        stack = stk_template(1000, 1000, "floorplan.flp", 2e-5, 2e-5, 300)
        (self.recording / "ice.stk").write_text(stack)
        # Snapshot extraction validates archive hashes, without opening activity arrays.
        (self.recording / "activity.npz").write_bytes(b"unused by saved-power extraction")
        self.paths = ["/chip/logic", "/chip/memory"]
        checksums = {name: digest(self.recording / name) for name in
                     ("activity.npz", "system_config.json", "ice.stk", "floorplan.flp")}
        write_json(self.recording / "recording.json", {
            "format": "softhier-thermal-replay-v1", "component_paths": self.paths,
            "sha256": checksums, "power_interval_ps": 20_000_000, "source_run": "test_b1_constant"})
        self.runs = {}
        for profile, leakage in (("constant", [.01, .02]), ("temperature_aware", [.02, .04])):
            run = self.directory / profile
            run.mkdir()
            self.runs[profile] = run
            (run / "floorplan.flp").write_text(floorplan)
            (run / "ice.stk").write_text(stack)
            write_json(run / "summary.json", {
                "status": "time_limit", "recording": str(self.recording), "profile": profile,
                "single_pass": False, "slots": 2, "slot_ps": 20_000_000, "step_ps": 20_000_000,
                "recording_sha256": checksums})
            write_json(run / "components.json", {"paths": self.paths, "mapping": document["thermal_feedback"]["components"]})
            self.write_history(run, "dynamic_power_w", [[.1, .2], [.3, .5]])
            self.write_history(run, "leakage_power_w", [leakage, leakage])
            with (run / "timeseries.csv").open("w", newline="") as stream:
                csv.writer(stream).writerows([
                    ["time_ps", "dynamic_w", "leakage_w", "constant_w", "peak_component_c"],
                    [20_000_000, .3, sum(leakage), .04, 30], [40_000_000, .8, sum(leakage), .04, 31]])

    def write_history(self, run, name, rows, paths=None):
        with gzip.open(run / (name + ".csv.gz"), "wt", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["time_ps", *(paths or self.paths)])
            writer.writerows([i * 20_000_000, *row] for i, row in enumerate(rows, 1))

    def test_export_preserves_geometry_order_and_accounts_for_fixed_power_once(self):
        before = {p: digest(p) for p in self.directory.rglob("*") if p.is_file()}
        output = self.directory / "export"
        metadata = export(self.runs["constant"], output, "4e-5")
        self.assertEqual(metadata["interval_start_ps"], 20_000_000)
        self.assertEqual(metadata["interval_end_ps"], 40_000_000)
        self.assertAlmostEqual(metadata["dynamic_w"], .8)
        self.assertAlmostEqual(metadata["leakage_w"], .03)
        self.assertAlmostEqual(metadata["constant_w"], .04)
        self.assertAlmostEqual(metadata["total_w"], .87)
        self.assertAlmostEqual(metadata["average_power_density_w_per_mm2"], .87)
        self.assertEqual(floorplan_blocks((output / "power_map.flp").read_text()),
                         floorplan_blocks((self.recording / "floorplan.flp").read_text()))
        import re
        values = re.findall(r"(?m)^\s*power values\s+([^;]+);", (output / "power_map.flp").read_text())
        self.assertEqual(list(map(float, values)), [.52, .04, .31])
        with (output / "power_map_regions.csv").open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([r["name"] for r in rows], ["chip__memory", "chip__fixed", "chip__logic"])
        self.assertEqual(float(rows[1]["dynamic_w"]), 0)
        self.assertEqual(float(rows[1]["leakage_w"]), 0)
        self.assertEqual({p: digest(p) for p in before}, before)

    def test_exact_endpoints_and_complete_histories_are_required(self):
        run = self.runs["constant"]
        for seconds in ("0", "-1", "nan", "inf", "0.0000000000001", "0.00003", "0.00006"):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                read_snapshot(run, seconds)
        self.assertEqual(time_ps("0.99998"), 999_980_000_000)
        path = run / "dynamic_power_w.csv.gz"
        original = path.read_bytes()
        corruptions = (
            "",
            "time_ps,/chip/logic,/chip/memory",
            "time_ps,/chip/memory,/chip/logic\n20000000,.1,.2\n",
            "time_ps,/chip/logic,/chip/memory\n20000001,.1,.2\n",
            "time_ps,/chip/logic,/chip/memory\n20000000,.1,.2\n20000000,.3,.5\n",
            "time_ps,/chip/logic,/chip/memory\n20000000,.1,.2\n40000000,.3",
            "time_ps,/chip/logic,/chip/memory\n20000000,.1,.2\n40000000,nan,.5\n",
            "time_ps,/chip/logic,/chip/memory\n20000000,.1,.2\n40000000,-.3,.5\n",
            "time_ps,/chip/logic,/chip/memory\n20000000,.1,.2\n",
        )
        for contents in corruptions:
            with self.subTest(contents=contents):
                with gzip.open(path, "wt") as stream:
                    stream.write(contents)
                with self.assertRaises(ValueError):
                    read_snapshot(run, "4e-5")
        path.write_bytes(original)

    def test_inconsistent_totals_and_mutated_recordings_are_rejected(self):
        run = self.runs["constant"]
        self.write_history(run, "dynamic_power_w", [[.1, .2], [.4, .5]])
        with self.assertRaisesRegex(ValueError, "totals differ"):
            read_snapshot(run, "4e-5")
        (self.recording / "floorplan.flp").write_text("changed")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            read_snapshot(run, "4e-5")

    def test_pair_rejects_different_cooling_or_component_activity(self):
        constant = read_snapshot(self.runs["constant"], "4e-5")
        aware = read_snapshot(self.runs["temperature_aware"], "4e-5")
        validate_pair(constant, aware)
        aware.dynamic[:] = [.5, .3]  # Equal total power does not imply equal activity.
        with self.assertRaisesRegex(ValueError, "dynamic activity"):
            validate_pair(constant, aware)
        aware.dynamic[:] = constant.dynamic
        aware.stack["heat_transfer_coefficient_w_per_m2_k"] = 100
        with self.assertRaisesRegex(ValueError, "thermal stacks"):
            validate_pair(constant, aware)

    def test_plot_uses_common_regional_power_scale_and_separate_density(self):
        output = self.directory / "pair"
        metadata = plot(self.runs["constant"], self.runs["temperature_aware"], output, "4e-5")
        self.assertEqual(metadata["colormap"], "turbo")
        self.assertEqual(metadata["color_limits_mw"], [0, 540])
        svg = (output / "power_maps.svg").read_text()
        for text in ("Region power (mW)", "Total power: 0.870 W", "Avg. density: 0.870 W/mm²",
                     "Dynamic: 0.800 W | Leakage: 0.030 W | Fixed: 0.040 W"):
            self.assertIn(text, svg)
        self.assertEqual(len(floorplan_blocks((output / "temperature_aware_power_map.flp").read_text())), 3)

    def test_source_outputs_and_single_pass_runs_are_protected(self):
        run = self.runs["constant"]
        for output in (run, run / "figures", self.recording / "figures"):
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, "outside"):
                export(run, output, "4e-5")
        path = run / "summary.json"
        summary = json.loads(path.read_text())
        summary["single_pass"] = True
        write_json(path, summary)
        with self.assertRaisesRegex(ValueError, "partial power windows"):
            read_snapshot(run, "4e-5")

    def test_stack_merges_only_consecutive_compatible_tim_and_pcb(self):
        stack = read_stack(self.recording / "ice.stk")
        self.assertEqual(len(stack["layers_top_to_bottom"]), 11)
        self.assertAlmostEqual(stack["heat_transfer_coefficient_w_per_m2_k"], 15000)
        self.assertEqual(stack["total_thickness_um"], 1180)
        blocks = schematic_blocks(stack)
        self.assertEqual([b["name"] for b in blocks], ["Heatsink", "TIM", "Silicon die", "Bumps", "PCB"])
        self.assertEqual([b["thickness_um"] for b in blocks], [50, 30, 50, 50, 1000])
        changed = dict(stack)
        changed["layers_top_to_bottom"] = [dict(layer) for layer in stack["layers_top_to_bottom"]]
        changed["layers_top_to_bottom"][2]["material"] = "CU"
        self.assertEqual([b["thickness_um"] for b in schematic_blocks(changed)[1:4]], [7.5, 7.5, 15])
        changed["layers_top_to_bottom"] = [stack["layers_top_to_bottom"][1], stack["layers_top_to_bottom"][5],
                                          stack["layers_top_to_bottom"][2]]
        self.assertEqual([b["name"] for b in schematic_blocks(changed)], ["TIM", "Silicon die", "TIM"])
        metadata = plot_stack(self.recording / "ice.stk", self.directory / "stack")
        self.assertEqual(metadata["blocks_top_to_bottom"], blocks)
        self.assertIn("Footprint: 1.000 × 1.000 mm²", (self.directory / "stack/layer_stack.svg").read_text())

    def test_stack_rejects_missing_layers_and_unsupported_die_instead_of_omitting_them(self):
        path = self.recording / "ice.stk"
        original = path.read_text()
        for text in (original.replace("TIM_LAYER ;", "UNKNOWN ;", 1),
                     original.replace("source  50 SILICON ;", "source 50 SILICON ; layer 50 SILICON ;"),
                     original.replace("height 7.5", "height -7.5")):
            with self.subTest(text=text):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    read_stack(path)


if __name__ == "__main__":
    unittest.main()
