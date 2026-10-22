"""Portable two-die recordings, native transport, and separate die analysis."""

import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from Interface_scripts.tests.test_memory_on_logic import contract as multidie_contract, thermal_inputs, GENERATOR, ROOT, SERVER
from Interface_scripts.tests.test_thermal_replay import contract as legacy_contract
import numpy as np

from experiments.thermal_replay.activity import LeakageModel, load_recording, write_json
from experiments.thermal_replay.ice import IceClient
from experiments.thermal_replay.layout import Layout
from experiments.thermal_replay.record import capture, record
from experiments.thermal_replay.replay import Mapping, run
from experiments.thermal_replay.validate import compare_replay


def source_fixture(folder, stack_order="dram_on_logic"):
    """Three windows, a partial final window, and changing DRAM background."""
    folder = Path(folder)
    generated = folder / "generated/3dice"
    generated.mkdir(parents=True)
    (folder / "traces").mkdir()
    (folder / "logs").mkdir()
    document = multidie_contract(1, 1, thermal_config=thermal_inputs(stack_order))
    document["metadata"]["power_model_specs"] = legacy_contract()["metadata"]["power_model_specs"]
    for element in document["floorplan"]["elements"]:
        if "constant_w" in element["power"]:
            element["power"]["constant_w"] = .001
    write_json(folder / "generated/system_config.json", document)
    subprocess.run([sys.executable, str(GENERATOR), "--system-config", str(folder / "generated/system_config.json"),
                    "--geo", str(generated / "geo.json"), "--floorplan", str(generated / "floorplan.flp"),
                    "--stk", str(generated / "ice.stk"), "--power-interval-ps", "20000000",
                    "--target-top-die-cells", "32"], check=True, capture_output=True, text=True)
    entries = [{"protocol": {"name": "gvsoc-power-hook", "version": 1}, "phase": "init",
                "temperature_unit": "celsius", "window": {"start_ps": 0, "end_ps": 0}, "components": []}]
    edges = [0, 20_000_000, 40_000_000, 55_000_000]
    for slot, (start, end) in enumerate(zip(edges, edges[1:])):
        components = []
        for i, component in enumerate(document["thermal_feedback"]["components"]):
            dram = component["die"] == "DRAM_DIE"
            dynamic = (.002, .004, .001)[slot] if dram else .0002 * (i % 3 + 1) * (slot + 1)
            leakage = (.001, .003, .002)[slot] if dram else .0001 * (i % 3 + 1)
            components.append({"path": component["path"], "scope": "subtree", "dynamic_w": dynamic,
                               "leakage_w": leakage, "total_w": dynamic + leakage, "temperature_c": 26.85})
        entries.append({"protocol": {"name": "gvsoc-power-hook", "version": 1},
                        "phase": "final" if slot == 2 else "update", "temperature_unit": "celsius",
                        "window": {"start_ps": start, "end_ps": end}, "components": components})
    (folder / "traces/power_hook_trace.jsonl").write_text("\n".join(map(json.dumps, entries)) + "\n")
    (folder / "logs/simulator.log").write_text("THERMAL_REPLAY_ROI_START_PS=5000000\nTHERMAL_REPLAY_ROI_END_PS=45000000\n")
    return document


class MultiDieReplayTests(unittest.TestCase):
    stack_order = "dram_on_logic"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="multidie-replay-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.source = self.folder / "source"
        self.document = source_fixture(self.source, self.stack_order)
        self.recording = self.folder / "recording"
        record(self.source, self.recording)

    def test_archive_relocates_without_source_and_checks_all_inputs(self):
        moved = self.folder / "relocated"
        shutil.move(self.recording, moved)
        shutil.rmtree(self.source)
        manifest, document, activity = load_recording(moved)
        self.assertEqual(manifest["format"], "softhier-thermal-replay-v2")
        self.assertEqual(activity.period, 40_000_000)
        layout = Layout(moved, document)
        expected_order = (["LOGIC_DIE", "DRAM_DIE"] if self.stack_order == "dram_on_logic"
                          else ["DRAM_DIE", "LOGIC_DIE"])
        self.assertEqual([d["id"] for d in layout.dies], expected_order)
        self.assertEqual(document["metadata"]["thermal_config"]["stack_order"], self.stack_order)
        for die in layout.dies:
            self.assertEqual(die["floorplan"].parent, moved)
            self.assertIn(die["floorplan"].name, manifest["sha256"])
        mapping = Mapping(document, moved)
        values = np.arange(len(manifest["component_paths"]), dtype=float)
        powers = mapping.powers(values)
        self.assertEqual(powers[-1 if self.stack_order == "dram_on_logic" else 0], values[-1])
        self.assertAlmostEqual(powers.sum(), values.sum() + .001)
        region_temperatures = np.full(len(layout.entries), 40.)
        region_temperatures[mapping.region_groups["DRAM_DIE"]] = 80.
        component_temperatures = mapping.temperatures(region_temperatures)
        np.testing.assert_array_equal(component_temperatures[:-1], np.full(len(values) - 1, 40.))
        self.assertEqual(component_temperatures[-1], 80.)
        if SERVER.is_file():
            self.assertEqual(run(moved, self.folder / "relocated_run", profile="constant", cycles=1)["status"], "complete")
        layout.dies[1]["floorplan"].write_text(layout.dies[1]["floorplan"].read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "checksum"):
            load_recording(moved)

    def test_manifest_must_follow_native_source_order(self):
        path = self.recording / "thermal_manifest.json"
        manifest = json.loads(path.read_text())
        manifest["dies"].reverse()
        manifest["power_order"] = [e for d in manifest["dies"] for e in d["elements"]]
        write_json(path, manifest)
        with self.assertRaisesRegex(ValueError, "die order"):
            Layout(self.recording, self.document)

    def test_only_logic_leakage_uses_temperature_factors(self):
        paths = [c["path"] for c in self.document["thermal_feedback"]["components"]]
        model = LeakageModel(self.document, paths)
        for temperature in (25, 80, 180):
            factors = model.factors(np.full(len(paths), temperature))
            self.assertEqual(factors[-1], 1.)
            self.assertEqual(int(model.temperature_dependent.sum()), len(paths) - 1)
            if temperature > 25:
                self.assertTrue((factors[:-1] > 1).all())
        bad = copy.deepcopy(self.document)
        bad["metadata"]["dram_power_model"]["temperature_dependence"] = "unknown"
        with self.assertRaisesRegex(ValueError, "DRAM"):
            LeakageModel(bad, paths)

    def test_hot_dram_does_not_trigger_the_logic_model_limit(self):
        layout = Layout(self.recording, self.document)
        dram_regions = [layout.entries.index(name) for die in layout.dies if die["id"] == "DRAM_DIE"
                        for name in die["elements"]]
        class HotDramClient:
            count = len(layout.entries)
            state_calls = 0
            def __init__(self, *args, **kwargs):
                self.slots = 0
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def state(self):
                type(self).state_calls += 1
                return np.full(self.count * 3, 26.85)
            def temperatures(self, expected):
                values = np.full(self.count, 26.85)
                if self.slots:
                    values[dram_regions] = 180.
                return values
            def advance(self, powers):
                self.slots += 1
        with patch("experiments.thermal_replay.replay.IceClient", HotDramClient):
            summary = run(self.recording, self.folder / "hot_dram", server=Path(__file__), cycles=2)
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["per_die"]["DRAM_DIE"]["peak_component_c"], 180)
        self.assertEqual(HotDramClient.state_calls, 2)

    def test_capture_uses_combined_preload_and_keeps_dram_voltage(self):
        artifacts = self.source / "artifacts"
        (artifacts / "environment").mkdir(parents=True)
        for filename in ("layer.elf", "preload.elf", "arch.py"):
            (artifacts / filename).touch()
        (self.source / "run.env").write_text("SOFTHIER_INPUT_PRELOAD=''\nPOWER_INTERVAL_PS=20000000\n")
        provider = self.folder / "provider.py"
        # A provider stub validates capture inputs and emits the known trace;
        # no GVSoC, workload build, or submodule edits are involved.
        provider.write_text(f'#!{sys.executable}\n' + '''import hashlib, json, os, pathlib, shutil, sys
env = os.environ
source = pathlib.Path(env["SIMULATOR_CONFIG"]).parent.parent
assert pathlib.Path(env["SOFTHIER_BINARY"]).name == "layer.elf"
assert pathlib.Path(env["SIMULATOR_PLATFORM"]).name == "preload.elf"
assert env["SOFTHIER_INPUT_PRELOAD"] == ""
for stem in ("THERMAL", "DRAM_POWER"):
    assert json.loads(pathlib.Path(env[f"SOFTHIER_{stem}_CONFIG"]).read_text())
if sys.argv[1] == "export-system":
    document = json.loads((source / "generated/system_config.json").read_text())
    thermal = document["metadata"]["thermal_config"]
    thermal.setdefault("stack_order", "dram_on_logic")
    override = pathlib.Path(__file__).with_name("export_override.json")
    if override.is_file():
        thermal.update(json.loads(override.read_text()))
    document["metadata"]["thermal_config_sha256"] = hashlib.sha256(
        json.dumps(thermal, sort_keys=True).encode()).hexdigest()
    pathlib.Path(env["SYSTEM_CONFIG_FILE"]).write_text(json.dumps(document))
else:
    updates = json.loads(pathlib.Path(env["POWER_HOOK_CONFIG_FILE"]).read_text())["temperatures"]
    assert all(update["voltage_v"] == .7 for update in updates[:-1])
    assert "voltage_v" not in updates[-1]
    shutil.copyfile(source / "traces/power_hook_trace.jsonl", env["POWER_HOOK_TRACE_FILE"])
    print((source / "logs/simulator.log").read_text())
''')
        provider.chmod(0o755)
        output = self.folder / "captured"
        result = capture(self.source, output, provider)
        self.assertEqual(result["format"], "softhier-thermal-replay-v2")
        load_recording(output / "recording")
        if self.stack_order == "dram_on_logic":
            legacy = copy.deepcopy(self.document)
            del legacy["metadata"]["thermal_config"]["stack_order"]
            legacy["metadata"]["thermal_config_sha256"] = hashlib.sha256(
                json.dumps(legacy["metadata"]["thermal_config"], sort_keys=True).encode()).hexdigest()
            write_json(self.source / "generated/system_config.json", legacy)
            legacy_output = self.folder / "captured_legacy"
            capture(self.source, legacy_output, provider)
            _, archived, _ = load_recording(legacy_output / "recording")
            self.assertEqual(archived, legacy, "capture must preserve the archived model")
        changed_bond = dict(self.document["metadata"]["thermal_config"]["face_to_face_bond"], thickness_um=3)
        other_order = "logic_on_dram" if self.stack_order == "dram_on_logic" else "dram_on_logic"
        for key, value in (("stack_order", other_order), ("face_to_face_bond", changed_bond)):
            with self.subTest(changed_input=key):
                write_json(self.folder / "export_override.json", {key: value})
                with self.assertRaisesRegex(ValueError, "metadata differs"):
                    capture(self.source, self.folder / f"reject_{key}", provider)

    @unittest.skipUnless(SERVER.is_file(), "requires built 3D-ICE server")
    def test_native_pair_analysis_exports_and_gifs(self):
        from experiments.thermal_replay import analyze, power_maps, plot_stack
        summaries = {}
        for profile in ("constant", "temperature_aware"):
            summaries[profile] = run(self.recording, self.folder / profile, profile=profile, cycles=3)
        constant = analyze.ReplayRun(self.folder / "constant", "constant")
        aware = analyze.ReplayRun(self.folder / "temperature_aware", "temperature_aware")
        analyze.validate_pair(constant, aware)
        np.testing.assert_array_equal(constant.die_power["DRAM_DIE"]["leakage"], aware.die_power["DRAM_DIE"]["leakage"])
        self.assertGreater(np.ptp(constant.die_power["DRAM_DIE"]["leakage"]), 0)
        self.assertGreater(aware.energy["leakage"][:-1].sum(), constant.energy["leakage"][:-1].sum())
        for summary in summaries.values():
            self.assertEqual(summary["status"], "complete")
            self.assertAlmostEqual(sum(d["total_energy_j"] for d in summary["per_die"].values()), summary["total_energy_j"])
        seconds = str(constant.duration)
        power_maps.export(self.folder / "constant", self.folder / "export", seconds)
        for die in ("logic_die", "dram_die"):
            self.assertTrue((self.folder / "export" / die / "power_map.flp").is_file())
        plotted = power_maps.plot(self.folder / "constant", self.folder / "temperature_aware", self.folder / "power", seconds)
        self.assertEqual(set(plotted["per_die"]), {"LOGIC_DIE", "DRAM_DIE"})
        scales = [value["color_limits_mw"] for value in plotted["per_die"].values()]
        self.assertEqual(scales[0], scales[1])
        with self.assertRaisesRegex(ValueError, "unknown die"):
            power_maps.export(self.folder / "constant", self.folder / "bad", seconds, "NO_DIE")
        output = self.folder / "analysis"
        output.mkdir()
        figures = analyze.Figures(output, constant, aware)
        name = figures.animation(aware, "DRAM_DIE")
        self.assertTrue((output / name).is_file())
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots()
        collection = figures.map_panel(ax, aware.map_frames[-1], die="DRAM_DIE")
        self.assertEqual(len(collection.get_paths()), 1)
        plt.close(fig)
        stack = plot_stack.read_stack(self.recording / "ice.stk")
        self.assertEqual(len(stack["layers_top_to_bottom"]), 17)
        self.assertEqual(stack["total_thickness_um"], 1242)
        blocks = plot_stack.schematic_blocks(stack)
        self.assertEqual([b["thickness_um"] for b in blocks], [50, 30, 50, 5, 2, 5, 50, 50, 1000])
        expected = ["DRAM silicon", "DRAM BEOL", "Hybrid bond", "Logic BEOL", "Logic silicon"]
        if self.stack_order == "logic_on_dram":
            expected.reverse()
        self.assertEqual([b["name"] for b in blocks[2:7]], expected)
        self.assertTrue(all(not b["powered"] for b in blocks[3:6]))
        self.assertEqual(sum(layer["powered"] for layer in stack["layers_top_to_bottom"]), 2)
        plot_stack.plot_stack(self.recording / "ice.stk", self.folder / "stack_plot")
        saved = json.loads((self.folder / "stack_plot/layer_stack.json").read_text())
        self.assertEqual([b["name"] for b in saved["blocks_top_to_bottom"][2:7]], expected)
        self.assertTrue((self.folder / "stack_plot/layer_stack.png").is_file())

    @unittest.skipUnless(SERVER.is_file(), "requires built 3D-ICE server")
    def test_single_pass_matches_file_mode_on_both_dies(self):
        recording = self.folder / "whole"
        record(self.source, recording, whole_trace=True)
        _, document, activity = load_recording(recording)
        mapping = Mapping(document, self.source / "generated/3dice")
        powers = [mapping.powers(d + l) for d, l in zip(activity.dynamic, activity.leakage)]
        power_path = self.source / "generated/3dice/power.txt"
        power_path.write_text("\n".join(" ".join(map(str, row)) for row in powers + [np.full(len(mapping.entries), -1)]) + "\n")
        subprocess.run([str(SERVER), "ice.stk", "--power-trace", "power.txt", "--until-minus-one"],
                       cwd=power_path.parent, check=True, capture_output=True, timeout=30)
        results = self.source / "results/3dice"
        results.mkdir(parents=True)
        for die in mapping.layout.dies:
            shutil.copyfile(power_path.parent / die["temperature_output"], results / die["temperature_output"])
        step = float(re.search(r"transient step ([^,]+)", (power_path.parent / "ice.stk").read_text())[1])
        output = self.folder / "single"
        run(recording, output, profile="recorded", single_pass=True, thermal_step=step)
        report = compare_replay(output, self.source, single_pass=True)
        self.assertTrue(report["passed"], report)
        self.assertEqual(set(report["per_die"]), {"LOGIC_DIE", "DRAM_DIE"})


class LogicOnDramReplayTests(MultiDieReplayTests):
    """Exercise recording, native replay, and analysis with logic nearest cooling."""
    stack_order = "logic_on_dram"


class MultiDieTransportTests(unittest.TestCase):
    def test_identifies_reordered_outputs_and_rejects_corruption(self):
        dies = [{"temperature_output": "logic.txt", "elements": ["logic"]},
                {"temperature_output": "dram.txt", "elements": ["dram"]}]
        def point(die, kelvin):
            filename = die["temperature_output"].encode()
            header = ("% Time(s) " + " ".join(e + "(K)" for e in die["elements"])).encode()
            pad = lambda data: data + b"\0" * (-len(data) % 4)
            return struct.pack("=III", len(filename), len(header), 1) + pad(filename) + pad(header) + struct.pack("=f", kelvin)
        good = struct.pack("=fI", 0, 2) + point(dies[1], 320) + point(dies[0], 310)
        client = IceClient(SERVER, ".")
        with patch.object(client, "exchange", return_value=good):
            np.testing.assert_allclose(client.temperatures(dies), [36.85, 46.85])
        broken = [good[:-1], good + b"xxxx", struct.pack("=fI", 0, 1) + point(dies[0], 310),
                  struct.pack("=fI", 0, 2) + point(dies[0], 310) * 2,
                  struct.pack("=fI", 0, 2) + point(dict(dies[1], elements=["wrong"]), 320) + point(dies[0], 310)]
        for payload in broken:
            with self.subTest(payload=payload), patch.object(client, "exchange", return_value=payload):
                with self.assertRaises(RuntimeError):
                    client.temperatures(dies)


if __name__ == "__main__":
    unittest.main()
