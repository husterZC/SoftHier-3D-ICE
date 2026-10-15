"""Multi-die mapping and face-to-face stack regressions using synthetic inputs."""

import copy
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Interface_scripts"))
from providers.softhier import export_system_config as exporter
from providers.softhier.thermal_config import DEFAULT_THERMAL_CONFIG, load_thermal_config, validate_thermal_config
from system_contract import ContractError, geometry_rectangle, power_order_elements, validate_contract
from thermal_manifest import copy_inputs, load_manifest

HOOK_SPEC = importlib.util.spec_from_file_location("multi_die_hook", ROOT / "Interface_scripts/co-simulation/3dice_power_hook.py")
HOOK = importlib.util.module_from_spec(HOOK_SPEC)
HOOK_SPEC.loader.exec_module(HOOK)

GENERATOR = ROOT / "Interface_scripts/geometry_generator/generate_3dice_inputs.py"
SERVER = ROOT / "3D-ICE/bin/3D-ICE-Server"
ACTUAL_DRAM3D_ARCH = ROOT / "SoftHier/soft_hier_sdk/apps_dram3d/config/arch.py"


def architecture(columns=4, rows=4):
    return SimpleNamespace(num_cluster_x=columns, num_cluster_y=rows, num_core_per_cluster=5,
        spatz_attaced_core_list=[0, 1, 2, 3], spatz_num_vlsu_port=8, spatz_num_function_unit=4,
        redmule_ce_height=2, redmule_ce_width=2, cluster_tcdm_size=128,
        noc_link_width=64, idma_outstand_txn=2, instruction_mem_size=128,
        cluster_stack_size=128, cluster_tcdm_bank_width=32, cluster_tcdm_bank_nb=4,
        dram3d_enable=1, dram3d_type="hbm2-example.json", hbm_chan_placement=[0, 0, 0, 0])


def contract(columns=4, rows=4, **kwargs):
    # Tests exercise geometry without requiring initialized simulator submodules.
    models = SimpleNamespace(technology_spec=lambda node: {"cell_um2_per_kge": 1, "nominal_voltage_v": .7},
                             logic_area_kge=lambda kind, **parameters: 10, model_spec=lambda name: {})
    with patch.object(exporter, "power_models", return_value=models):
        return exporter.build_contract(architecture(columns, rows), ROOT / "synthetic_arch.py", 0, **kwargs)


class MemoryOnLogicTests(unittest.TestCase):
    def temporary(self):
        base = ROOT / "tmp/interface_tests"
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def generate(self, document=None, target_cells=32, interval_ps=1000000000):
        folder = self.temporary()
        document = contract(1, 1) if document is None else document
        source = folder / "system.json"
        source.write_text(json.dumps(document))
        result = subprocess.run([sys.executable, str(GENERATOR), "--system-config", str(source),
            "--geo", str(folder / "geo.json"), "--floorplan", str(folder / "floorplan.flp"),
            "--stk", str(folder / "ice.stk"), "--power-interval-ps", str(interval_ps),
            "--target-top-die-cells", str(target_cells)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return folder

    def test_architecture_inventory_and_cluster_channel_alignment(self):
        document = contract()
        self.assertEqual(document["contract"]["version"], 2)
        self.assertEqual(len(document["power_trace"]["columns"]), 320)
        self.assertEqual(len(document["floorplan"]["elements"]), 336)
        self.assertEqual(len(document["thermal_feedback"]["components"]), 320)
        for cid in range(16):
            self.assertEqual(geometry_rectangle(document["geometry"], f"chip/cluster_{cid}"),
                             geometry_rectangle(document["geometry"], f"dram/channel_{cid}"))
        logic = document["thermal_feedback"]["components"][:-16]
        dram = document["thermal_feedback"]["components"][-16:]
        self.assertTrue(all(item["voltage_v"] == .7 for item in logic))
        self.assertTrue(all("voltage_v" not in item and item["die"] == "DRAM_DIE" for item in dram))
        self.assertEqual(dram[-1]["path"], "/chip/dram3d_chan_15")
        self.assertIn("provenance", document["metadata"]["thermal_config"])
        self.assertIn("provenance", document["metadata"]["dram_power_config"])

    def test_defaults_are_explicit_and_overrides_preserve_die_thickness(self):
        inputs = load_thermal_config(DEFAULT_THERMAL_CONFIG)
        self.assertIn("not measured", inputs["provenance"])
        inputs["dram_die"]["thickness_um"] = 75
        inputs["dram_die"]["source_thickness_um"] = 2
        document = contract(1, 1, thermal_config=inputs)
        layers = document["thermal_stack"]["layers"]
        self.assertEqual(sum(layer["thickness_um"] for layer in layers if layer.get("die") == "DRAM_DIE"), 75)
        self.assertEqual(next(layer["thickness_um"] for layer in layers if layer["id"] == "DRAM_BULK"), 73)
        for key, value in (("thickness_um", 0), ("source_thickness_um", 75), ("thermal_conductivity_w_m_k", float("nan"))):
            bad = copy.deepcopy(inputs)
            bad["dram_die"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_thermal_config(bad)
        del inputs["face_to_face_bond"]["thickness_um"]
        with self.assertRaisesRegex(ValueError, "face_to_face_bond.thickness_um"):
            validate_thermal_config(inputs)

    def test_validator_rejects_cross_die_mapping_overlap_and_wrong_source(self):
        document = contract(2, 1)
        bad = copy.deepcopy(document)
        bad["thermal_feedback"]["components"][-1]["die"] = "LOGIC_DIE"
        with self.assertRaisesRegex(ContractError, "own die"):
            validate_contract(bad)
        bad = copy.deepcopy(document)
        bad["geometry"]["dram"]["subs"]["channel_1"]["offset"] = [0, 0]
        with self.assertRaisesRegex(ContractError, "overlapping"):
            validate_contract(bad)
        bad = copy.deepcopy(document)
        bad["thermal_stack"]["dies"][1]["active_face"] = "top"
        with self.assertRaisesRegex(ContractError, "active_face"):
            validate_contract(bad)
        bad = copy.deepcopy(document)
        bad["thermal_stack"]["dies"][1]["total_thickness_um"] += 1
        with self.assertRaisesRegex(ContractError, "total thickness"):
            validate_contract(bad)

    def test_power_order_follows_sources_even_when_floorplan_order_changes(self):
        document = contract(1, 1)
        document["floorplan"]["elements"].reverse()
        validate_contract(document)
        ordered = power_order_elements(document)
        self.assertEqual(ordered[-1]["die"], "DRAM_DIE")
        self.assertTrue(all(item["die"] == "LOGIC_DIE" for item in ordered[:-1]))

    def test_generated_sources_face_the_bond_and_manifest_matches_files(self):
        folder = self.generate()
        text = (folder / "ice.stk").read_text()
        self.assertRegex(text, r"die LOGIC_DIE_IC\s*:\s*source 1 SILICON\s*;\s*layer 49 SILICON")
        self.assertRegex(text, r"die DRAM_DIE_IC\s*:\s*layer 49 DRAM\s*;\s*source 1 DRAM")
        stack = text.split("stack:", 1)[1].split("solver:", 1)[0]
        self.assertLess(stack.index("DRAM_DIE"), stack.index("F2F_BOND"))
        self.assertLess(stack.index("F2F_BOND"), stack.index("LOGIC_DIE"))
        self.assertRegex(text, r"material DRAM\s*:\s*thermal conductivity 0.00013\s*;\s*volumetric heat capacity 1.61e-12")
        self.assertIn("numofcores 1 ;", text)
        dimensions = re.search(r"chip length ([^,]+), width ([^;]+)", text)
        cells = re.search(r"cell length ([^,]+), width ([^;]+)", text)
        self.assertTrue(all(float(pitch) <= float(size) for pitch, size in zip(cells.groups(), dimensions.groups())),
                        "small dies must retain nonzero passive-layer discretization")
        manifest = json.loads((folder / "thermal_manifest.json").read_text())
        self.assertEqual([die["id"] for die in manifest["dies"]], ["LOGIC_DIE", "DRAM_DIE"])
        names = []
        for die in manifest["dies"]:
            floorplan = (folder / die["floorplan"]).read_text()
            self.assertEqual(re.findall(r"^(\S+)\s*:", floorplan, re.MULTILINE), die["elements"])
            names += die["elements"]
            self.assertIn(die["temperature_output"], text)
            self.assertIn(die["map_output"], text)
        self.assertEqual(names, manifest["power_order"])
        self.assertEqual(names[-1], "dram__channel_0")

    def test_single_die_contract_remains_version_one(self):
        arch = architecture(1, 1)
        arch.dram3d_enable = 0
        models = SimpleNamespace(technology_spec=lambda node: {"cell_um2_per_kge": 1, "nominal_voltage_v": .7},
                                 logic_area_kge=lambda kind, **parameters: 10, model_spec=lambda name: {})
        with patch.object(exporter, "power_models", return_value=models):
            document = exporter.build_contract(arch, ROOT / "synthetic_arch.py", 0)
        self.assertEqual(document["contract"]["version"], 1)
        self.assertNotIn("thermal_stack", document)
        self.assertTrue(all("die" not in item for item in document["floorplan"]["elements"]))
        folder = self.generate(document)
        manifest = json.loads((folder / "thermal_manifest.json").read_text())
        self.assertEqual(manifest["dies"][0]["id"], "TOP_DIE")
        self.assertEqual(manifest["dies"][0]["temperature_output"], "output_top_die_flp_avg.txt")

    @unittest.skipUnless(SERVER.is_file(), "requires the built 3D-ICE server")
    def test_native_solver_power_order_and_two_source_feedback(self):
        folder = self.generate()
        manifest = json.loads((folder / "thermal_manifest.json").read_text())
        count = len(manifest["power_order"])
        zeros = [0] * count
        document = json.loads((folder / "system.json").read_text())
        logic_elements = power_order_elements(document)[:-1]
        rectangles = [geometry_rectangle(document["geometry"], item["name"]) for item in logic_elements]
        areas = [width * height for _, _, width, height in rectangles]
        logic_heat = [.02 * area / sum(areas) for area in areas] + [0]
        for heated_die, powers in ((0, logic_heat), (1, [0] * (count - 1) + [.02])):
            with self.subTest(heated_die=heated_die):
                (folder / "power.txt").write_text("\n".join(" ".join(map(str, row)) for row in (zeros, powers, [-1] * count)) + "\n")
                result = subprocess.run([str(SERVER), "ice.stk", "--power-trace", "power.txt", "--until-minus-one"],
                                        cwd=folder, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                temperatures = []
                for die in manifest["dies"]:
                    rows = [[float(value) for value in line.split()] for line in (folder / die["temperature_output"]).read_text().splitlines()
                            if line.strip() and not line.startswith("%")]
                    self.assertEqual([len(row) for row in rows], [1 + len(die["elements"])] * 2)
                    self.assertTrue(all(abs(value - 300) < .001 for value in rows[0][1:]))
                    self.assertTrue(all(math.isfinite(value) and value >= 300 for value in rows[1][1:]))
                    temperatures.append(sum(rows[1][1:]) / len(die["elements"]))
                self.assertGreater(temperatures[heated_die], temperatures[1 - heated_die])
                self.assertGreater(temperatures[1 - heated_die], 300)

    @unittest.skipUnless(SERVER.is_file() and ACTUAL_DRAM3D_ARCH.is_file(), "requires native solver and pinned DRAM3D SDK")
    def test_actual_four_by_four_package_preserves_equilibrium_and_heating_bound(self):
        # Small meshes did not reveal a parallel-factorization failure seen on
        # the real 4x4 package. Keep its actual layout and passive-layer grid.
        architecture = exporter.import_architecture(ACTUAL_DRAM3D_ARCH)
        document = exporter.build_contract(architecture, ACTUAL_DRAM3D_ARCH, 0)
        folder = self.generate(document, target_cells=1024, interval_ps=20_000_000)
        ordered = power_order_elements(document)
        totals, areas = {}, []
        for entry in ordered:
            rectangle = geometry_rectangle(document["geometry"], entry["name"])
            area = rectangle[2] * rectangle[3] if "column" in entry["power"] else 0
            areas.append(area)
            totals[entry["die"]] = totals.get(entry["die"], 0) + area
        powers = [(1.0 if entry["die"] == "LOGIC_DIE" else .25) * area / totals[entry["die"]]
                  for entry, area in zip(ordered, areas)]
        rows = [[0] * len(ordered)] + [powers] * 9 + [[-1] * len(ordered)]
        (folder / "power.txt").write_text("\n".join(" ".join(map(str, row)) for row in rows) + "\n")
        result = subprocess.run([str(SERVER), "ice.stk", "--power-trace", "power.txt", "--until-minus-one"],
                                cwd=folder, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest = json.loads((folder / "thermal_manifest.json").read_text())
        for die in manifest["dies"]:
            values = [[float(value) for value in line.split()[1:]]
                      for line in (folder / die["temperature_output"]).read_text().splitlines()
                      if line.strip() and not line.startswith("%")]
            self.assertEqual(len(values), 10)
            self.assertTrue(all(abs(value - 300) <= .001 for value in values[0]))
            self.assertTrue(all(math.isfinite(value) and value >= 299.999 for row in values for value in row),
                            "positive heat cannot drive this initially ambient package below ambient")
            self.assertGreater(max(values[-1]), 300)
            for line in (folder / die["map_output"]).read_text().splitlines():
                if line.strip() and not line.startswith("%"):
                    self.assertTrue(all(float(value) >= 299.999 for value in line.split()))


class MultiDieHookTests(unittest.TestCase):
    temporary = MemoryOnLogicTests.temporary
    generate = MemoryOnLogicTests.generate

    def setUp(self):
        self.document = contract(1, 1)
        self.folder = self.generate(self.document)
        self.manifest_path = self.folder / "thermal_manifest.json"
        self.manifest = json.loads(self.manifest_path.read_text())
        self.config_path = self.folder / "hook.json"
        result = subprocess.run([sys.executable, str(ROOT / "Interface_scripts/co-simulation/generate_power_hook_config.py"),
            "--output", str(self.config_path), "--system-config", str(self.folder / "system.json"),
            "--thermal-manifest", str(self.manifest_path), "--power-trace", str(self.folder / "power.txt"),
            "--temperature-history", str(self.folder / "history.csv"), "--poll-seconds", ".005", "--timeout-seconds", "3"],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.config = HOOK.validate_hook_config(json.loads(self.config_path.read_text()), self.config_path)
        for die in self.manifest["dies"]:
            (self.folder / die["temperature_output"]).write_text(
                "% Time(s) " + " ".join(name + "(K)" for name in die["elements"]) + "\n")

    def request(self, phase, start=0, end=0):
        components = []
        if phase != "init":
            for index, component in enumerate(self.document["thermal_feedback"]["components"]):
                components.append({"path": component["path"], "scope": "subtree", "dynamic_w": (index + 1) / 100,
                    "leakage_w": 0, "total_w": (index + 1) / 100, "temperature_c": 30})
            components.reverse()  # Mapping must follow declared paths, not request position.
        path = self.folder / f"request_{phase}.json"
        path.write_text(json.dumps({"protocol": {"name": "gvsoc-power-hook", "version": 1}, "phase": phase,
            "temperature_unit": "celsius", "window": {"start_ps": start, "end_ps": end}, "components": components}))
        return SimpleNamespace(phase=phase, request=str(path), response=str(self.folder / f"response_{phase}.json"),
                               config=str(self.config_path))

    def append_temperatures(self, die_index, value, *, complete=True):
        die = self.manifest["dies"][die_index]
        with (self.folder / die["temperature_output"]).open("a") as stream:
            # Native microsecond timestamps may be identical after rounding.
            stream.write("0.000 " + " ".join([str(value)] * len(die["elements"])) + ("\n" if complete else ""))

    def wait_power_rows(self, expected):
        path = self.folder / "power.txt"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if path.exists() and len(path.read_text().splitlines()) >= expected:
                return
            time.sleep(.005)
        self.fail("power hook did not append its power slot")

    def test_init_voltage_and_both_die_rows_required_for_each_slot(self):
        init = self.request("init")
        self.assertEqual(HOOK.run(init), 0)
        updates = json.loads(Path(init.response).read_text())["temperatures"]
        self.assertTrue(all(update["voltage_v"] == .7 for update in updates[:-1]))
        self.assertNotIn("voltage_v", updates[-1])
        with ThreadPoolExecutor(max_workers=1) as pool:
            update = self.request("update", 0, 10_000_000)
            future = pool.submit(HOOK.run, update)
            self.wait_power_rows(1)
            self.append_temperatures(0, 310)
            self.append_temperatures(1, 330, complete=False)
            time.sleep(.03)
            self.assertFalse(future.done(), "hook returned before the DRAM row was complete")
            with (self.folder / self.manifest["dies"][1]["temperature_output"]).open("a") as stream:
                stream.write("\n")
            self.assertEqual(future.result(timeout=3), 0)
            values = json.loads(Path(update.response).read_text())["temperatures"]
            self.assertTrue(all(abs(item["temperature_c"] - 36.85) < 1e-8 for item in values[:-1]))
            self.assertAlmostEqual(values[-1]["temperature_c"], 56.85)
            final = self.request("final", 10_000_000, 15_000_000)
            future = pool.submit(HOOK.run, final)
            self.wait_power_rows(2)
            self.append_temperatures(1, 340)
            time.sleep(.03)
            self.assertFalse(future.done(), "hook returned before the logic die advanced")
            self.append_temperatures(0, 320)
            self.assertEqual(future.result(timeout=3), 0)
        values = json.loads(Path(final.response).read_text())["temperatures"]
        self.assertAlmostEqual(values[0]["temperature_c"], 46.85)
        self.assertAlmostEqual(values[-1]["temperature_c"], 66.85)
        rows = [[float(value) for value in line.split()] for line in (self.folder / "power.txt").read_text().splitlines()]
        self.assertEqual(rows[0], rows[1])
        self.assertEqual(rows[0], [(index + 1) / 100 for index in range(19)] + [0, .2])
        self.assertEqual(rows[2], [-1] * 21)

    def test_mismatched_completed_slots_fail_before_appending_power(self):
        self.append_temperatures(0, 310)
        with self.assertRaisesRegex(HOOK.HookError, "same completed slot"):
            HOOK.run(self.request("update", 0, 10))
        self.assertFalse((self.folder / "power.txt").exists())

    def test_manifest_rejects_wrong_source_order_and_die_assignment(self):
        self.manifest["dies"].reverse()
        self.manifest["power_order"] = [name for die in self.manifest["dies"] for name in die["elements"]]
        self.manifest_path.write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(HOOK.HookError, "power order"):
            HOOK.thermal_outputs(self.config, self.document)
        self.manifest["dies"].reverse()
        self.manifest["power_order"] = [name for die in self.manifest["dies"] for name in die["elements"]]
        self.manifest["dies"][0]["id"] = "WRONG_DIE"
        self.manifest_path.write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(HOOK.HookError, "die assignment"):
            HOOK.thermal_outputs(self.config, self.document)

    def test_offline_adapter_and_runtime_copy_preserve_all_regions(self):
        destination = self.folder / "runtime/thermal_manifest.json"
        copy_inputs(self.manifest_path, destination)
        copied = load_manifest(destination)
        self.assertEqual(copied["power_order"], self.manifest["power_order"])
        for original, staged in zip(self.manifest["dies"], copied["dies"]):
            self.assertEqual((self.folder / original["floorplan"]).read_bytes(),
                             (destination.parent / staged["floorplan"]).read_bytes())
        raw = self.folder / "raw.txt"
        raw.write_text(" ".join(str(index + 1) for index in range(20)) + "\n")
        done = self.folder / "done"
        done.touch()
        output = self.folder / "adapted.txt"
        result = subprocess.run([sys.executable, str(ROOT / "Interface_scripts/co-simulation/ice_trace_adapter.py"),
            "--system-config", str(self.folder / "system.json"), "--thermal-manifest", str(destination),
            "--input", str(raw), "--output", str(output), "--done-file", str(done)],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [[float(value) for value in line.split()] for line in output.read_text().splitlines()]
        self.assertEqual(rows, [list(range(1, 20)) + [0, 20], [-1] * 21])

    def test_thermal_replay_rejects_missing_manifest_before_creating_capture(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("thermal replay requires numpy")
        sys.path.insert(0, str(ROOT))
        from experiments.thermal_replay.record import record, capture
        run_folder = self.folder / "run"
        (run_folder / "generated/3dice").mkdir(parents=True)
        (run_folder / "generated/system_config.json").write_text(json.dumps(self.document))
        (run_folder / "generated/3dice/ice.stk").write_text((self.folder / "ice.stk").read_text())
        for name in ("record", "capture"):
            output = self.folder / name
            with self.subTest(operation=name), self.assertRaisesRegex((ValueError, FileNotFoundError), "thermal_manifest"):
                if name == "record":
                    record(run_folder, output)
                else:
                    capture(run_folder, output, ROOT / "Interface_scripts/providers/softhier/provider.sh")
            self.assertFalse(output.exists())

    def test_runtime_copy_rewrites_only_stack_floorplans_after_relocation(self):
        exported = self.folder / "exported floorplans"
        exported.mkdir()
        stack = (self.folder / "ice.stk").read_text()
        for die in self.manifest["dies"]:
            original = self.folder / die["floorplan"]
            target = exported / original.name
            original.rename(target)
            relative = str(target.relative_to(self.folder))
            stack = stack.replace('"./' + die["floorplan"] + '"', '"' + relative + '"')
            die["floorplan"] = relative
        (self.folder / "ice.stk").write_text(stack)
        self.manifest_path.write_text(json.dumps(self.manifest))
        destination = self.folder / "runtime/thermal_manifest.json"
        runtime_stack = destination.parent / "renamed.stk"
        copy_inputs(self.manifest_path, destination, self.folder / "ice.stk", runtime_stack)
        copied = load_manifest(destination)
        original_text = (self.folder / "ice.stk").read_text()
        copied_text = runtime_stack.read_text()
        pattern = r'floorplan\s+"[^"]+"'
        self.assertEqual(re.sub(pattern, "floorplan PLACEHOLDER", original_text),
                         re.sub(pattern, "floorplan PLACEHOLDER", copied_text))
        for path in re.findall(r'floorplan\s+"([^"]+)"', copied_text):
            self.assertTrue((runtime_stack.parent / path).is_file())
            self.assertEqual(Path(path).parent, Path("."))
        self.assertEqual(copied["power_order"], self.manifest["power_order"])

    def test_single_die_runtime_floorplan_override_and_summary_maximum(self):
        document = copy.deepcopy(self.document)
        document["contract"]["version"] = 1
        del document["thermal_stack"]
        del document["geometry"]["dram"]
        document["floorplan"]["elements"].pop()
        document["power_trace"]["columns"].pop()
        document["thermal_feedback"]["components"].pop()
        for item in document["floorplan"]["elements"]:
            item.pop("die")
        for item in document["thermal_feedback"]["components"]:
            item.pop("die")
        folder = self.generate(document)
        destination = folder / "runtime/thermal_manifest.json"
        target = destination.parent / "custom floorplans/renamed.flp"
        copy_inputs(folder / "thermal_manifest.json", destination, folder / "ice.stk",
                    destination.parent / "ice.stk", target)
        staged = load_manifest(destination)
        self.assertEqual((destination.parent / staged["dies"][0]["floorplan"]).resolve(), target)
        self.assertIn('floorplan "custom floorplans/renamed.flp"', (destination.parent / "ice.stk").read_text())
        self.assertEqual(target.read_bytes(), (folder / "floorplan.flp").read_bytes())

        self.append_temperatures(0, 310)
        self.append_temperatures(1, 340)
        tool = ROOT / "Interface_scripts/thermal_manifest.py"
        summary = subprocess.run([sys.executable, str(tool), "summary", str(self.manifest_path)],
                                 check=True, capture_output=True, text=True)
        self.assertIn("LOGIC_DIE_max_temperature_k: 310.000", summary.stdout)
        self.assertIn("DRAM_DIE_max_temperature_k: 340.000", summary.stdout)
        self.assertRegex(summary.stdout, r"(?m)^max_temperature_k: 340.000$")
        gifs = subprocess.run([sys.executable, str(tool), "gif-paths", str(self.manifest_path),
                               "--gif", str(self.folder / "map.gif")], check=True, capture_output=True, text=True)
        self.assertIn("map_logic_die.gif", gifs.stdout)
        self.assertIn("map_dram_die.gif", gifs.stdout)


if __name__ == "__main__":
    unittest.main()
