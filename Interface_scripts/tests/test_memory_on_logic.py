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
from system_contract import ContractError, geometry_rectangle, power_order_elements, thermal_die_ids, validate_contract
from thermal_manifest import copy_inputs, load_manifest

HOOK_SPEC = importlib.util.spec_from_file_location("multi_die_hook", ROOT / "Interface_scripts/co-simulation/3dice_power_hook.py")
HOOK = importlib.util.module_from_spec(HOOK_SPEC)
HOOK_SPEC.loader.exec_module(HOOK)

GENERATOR = ROOT / "Interface_scripts/geometry_generator/generate_3dice_inputs.py"
SERVER = ROOT / "3D-ICE/bin/3D-ICE-Server"
ACTUAL_DRAM3D_ARCH = ROOT / "SoftHier/soft_hier_sdk/apps_dram3d/config/arch.py"
STACK_ORDERS = ("dram_on_logic", "logic_on_dram")


def thermal_inputs(stack_order):
    inputs = load_thermal_config(DEFAULT_THERMAL_CONFIG)
    inputs["stack_order"] = stack_order
    return inputs


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
        defaults = contract(1, 1)["thermal_stack"]
        self.assertEqual(len(defaults["layers"]), 17)
        self.assertEqual(sum(layer["thickness_um"] for layer in defaults["layers"]), 1242)
        self.assertEqual([die["total_thickness_um"] for die in defaults["dies"]], [55, 55])
        self.assertEqual([die["source_depth_um"] for die in defaults["dies"]], [5, 5])
        self.assertEqual([layer["id"] for layer in defaults["layers"][5:12]],
                         ["LOGIC_BULK", "LOGIC_SOURCE", "LOGIC_BEOL", "HYBRID_BOND",
                          "DRAM_BEOL", "DRAM_SOURCE", "DRAM_BULK"])
        self.assertEqual([layer["thickness_um"] for layer in defaults["layers"][5:12]],
                         [49, 1, 5, 2, 5, 1, 49])
        inputs["dram_die"]["thickness_um"] = 75
        inputs["dram_die"]["source_thickness_um"] = 2
        inputs["dram_die"]["beol"]["thickness_um"] = 3
        document = contract(1, 1, thermal_config=inputs)
        layers = document["thermal_stack"]["layers"]
        self.assertEqual(sum(layer["thickness_um"] for layer in layers if layer.get("die") == "DRAM_DIE"), 75)
        self.assertEqual(next(layer["thickness_um"] for layer in layers if layer["id"] == "DRAM_BULK"), 70)
        self.assertEqual(document["thermal_stack"]["dies"][1]["source_depth_um"], 3)
        for key, value in (("thickness_um", 0), ("source_thickness_um", 75), ("thermal_conductivity_w_m_k", float("nan"))):
            bad = copy.deepcopy(inputs)
            bad["dram_die"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_thermal_config(bad)
        del inputs["face_to_face_bond"]["thickness_um"]
        with self.assertRaisesRegex(ValueError, "face_to_face_bond.thickness_um"):
            validate_thermal_config(inputs)

    def test_beol_inputs_require_materials_and_leave_positive_bulk(self):
        for die in ("logic_die", "dram_die"):
            for key, value in (("thickness_um", 0), ("thickness_um", 54),
                               ("thermal_conductivity_w_m_k", [1, 2]),
                               ("thermal_conductivity_w_m_k", [1, float("nan"), 2]),
                               ("volumetric_heat_capacity_j_m3_k", None)):
                bad = load_thermal_config(DEFAULT_THERMAL_CONFIG)
                bad[die]["beol"][key] = value
                with self.subTest(die=die, key=key, value=value), self.assertRaises(ValueError):
                    validate_thermal_config(bad)
            bad = load_thermal_config(DEFAULT_THERMAL_CONFIG)
            bad[die]["beol"] = None
            with self.assertRaisesRegex(ValueError, "beol must be an object"):
                validate_thermal_config(bad)
        bad = load_thermal_config(DEFAULT_THERMAL_CONFIG)
        bad["face_to_face_bond"]["technology"] = "unknown"
        with self.assertRaisesRegex(ValueError, "technology"):
            validate_thermal_config(bad)

    def test_stack_order_validation_and_archiving(self):
        baseline = contract(2, 1)
        omitted = load_thermal_config(DEFAULT_THERMAL_CONFIG)
        self.assertEqual(omitted.pop("stack_order"), "dram_on_logic")
        self.assertEqual(contract(2, 1, thermal_config=omitted), baseline)
        self.assertNotIn("stack_order", omitted, "validation must not mutate caller inputs")
        for value in (None, True, 0, "", "logic", "LOGIC_ON_DRAM", [], {}):
            with self.subTest(invalid=value), self.assertRaisesRegex(ValueError, "stack_order"):
                validate_thermal_config(thermal_inputs(value))
        for order, expected in (("dram_on_logic", ["LOGIC_DIE", "DRAM_DIE"]),
                                ("logic_on_dram", ["DRAM_DIE", "LOGIC_DIE"])):
            with self.subTest(order=order):
                document = contract(2, 1, thermal_config=thermal_inputs(order))
                for key in ("geometry", "floorplan", "power_trace", "thermal_feedback"):
                    self.assertEqual(document[key], baseline[key], key)
                self.assertEqual(document["metadata"]["thermal_config"]["stack_order"], order)
                self.assertEqual(document["metadata"]["architecture_variant"], "memory-on-logic")
                stack = document["thermal_stack"]
                self.assertEqual(thermal_die_ids(document), expected)
                self.assertEqual([die["id"] for die in stack["dies"]], expected)
                self.assertEqual([die["active_face"] for die in stack["dies"]], ["top", "bottom"])
                self.assertEqual(len(stack["layers"]), 17)
                self.assertEqual(sum(layer["thickness_um"] for layer in stack["layers"]), 1242)
                self.assertEqual(stack["materials"], baseline["thermal_stack"]["materials"])
                self.assertEqual(stack["cooling"], baseline["thermal_stack"]["cooling"])
                self.assertEqual(stack["layers"][:5], baseline["thermal_stack"]["layers"][:5])
                self.assertEqual(stack["layers"][-5:], baseline["thermal_stack"]["layers"][-5:])
                if order == "logic_on_dram":
                    self.assertNotEqual(document["metadata"]["thermal_config_sha256"],
                                        baseline["metadata"]["thermal_config_sha256"])

    def test_asymmetric_die_layers_follow_identity_and_active_face(self):
        for order in STACK_ORDERS:
            for logic_beol, dram_beol in ((True, True), (False, True), (True, False), (False, False)):
                with self.subTest(order=order, logic_beol=logic_beol, dram_beol=dram_beol):
                    inputs = thermal_inputs(order)
                    inputs["logic_die"].update(thickness_um=61, source_thickness_um=2)
                    inputs["dram_die"].update(thickness_um=73, source_thickness_um=4,
                                             thermal_conductivity_w_m_k=[110, 120, 130])
                    for name, enabled, depth in (("logic_die", logic_beol, 3), ("dram_die", dram_beol, 7)):
                        if enabled:
                            inputs[name]["beol"]["thickness_um"] = depth
                        else:
                            del inputs[name]["beol"]
                    document = contract(1, 1, thermal_config=inputs)
                    stack = document["thermal_stack"]
                    self.assertEqual(sum(layer["thickness_um"] for layer in stack["layers"]), 1266)
                    for die in stack["dies"]:
                        logic = die["id"] == "LOGIC_DIE"
                        name, material = ("LOGIC", "SILICON") if logic else ("DRAM", "DRAM")
                        total, source, depth = (61, 2, 3 if logic_beol else 0) if logic else (73, 4, 7 if dram_beol else 0)
                        expected = [(f"{name}_BULK", material, total - source - depth),
                                    (f"{name}_SOURCE", material, source)]
                        if depth:
                            expected.append((f"{name}_BEOL", f"{name}_BEOL", depth))
                        if die["active_face"] == "bottom":
                            expected.reverse()
                        layers = [layer for layer in stack["layers"] if layer.get("die") == die["id"]]
                        self.assertEqual([(layer["id"], layer["material"], layer["thickness_um"]) for layer in layers], expected)
                        self.assertEqual(die["total_thickness_um"], total)
                        self.assertEqual(die.get("source_depth_um", 0), depth)
                        self.assertEqual(sum(bool(layer.get("source")) for layer in layers), 1)
                    self.assertEqual(stack["materials"]["DRAM"]["thermal_conductivity_w_m_k"], [110, 120, 130])

    def test_legacy_config_without_beol_retains_original_stack(self):
        inputs = load_thermal_config(DEFAULT_THERMAL_CONFIG)
        del inputs["stack_order"]
        for die in ("logic_die", "dram_die"):
            del inputs[die]["beol"]
            inputs[die]["thickness_um"] = 50
        del inputs["face_to_face_bond"]["technology"]
        inputs["face_to_face_bond"]["thickness_um"] = 5
        inputs["face_to_face_bond"]["thermal_conductivity_w_m_k"] = [5.5, 5.5, 113.0]
        inputs["face_to_face_bond"]["volumetric_heat_capacity_j_m3_k"] = 1845000.0
        document = contract(1, 1, thermal_config=inputs)
        stack = document["thermal_stack"]
        self.assertEqual(len(stack["layers"]), 15)
        self.assertEqual(sum(layer["thickness_um"] for layer in stack["layers"]), 1235)
        self.assertTrue(all("source_depth_um" not in die for die in stack["dies"]))
        text = (self.generate(document) / "ice.stk").read_text()
        self.assertRegex(text, r"die LOGIC_DIE_IC\s*:\s*source 1 SILICON\s*;\s*layer 49 SILICON")
        self.assertRegex(text, r"die DRAM_DIE_IC\s*:\s*layer 49 DRAM\s*;\s*source 1 DRAM")
        self.assertIn("layer F2F_BOND F2F_BOND_LAYER", text)
        self.assertRegex(text, r"material F2F\s*:\s*thermal conductivity 5\.5e-06, 5\.5e-06, 0\.000113\s*;\s*volumetric heat capacity 1\.845e-12")
        self.assertNotIn("HYBRID_BOND", text)
        self.assertNotIn("BEOL", text)

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

    def test_source_depth_must_match_covering_layers_on_active_face(self):
        document = contract(1, 1)
        for die_index in (0, 1):
            for depth in (-1, float("nan"), True, 0, 4, 6):
                bad = copy.deepcopy(document)
                bad["thermal_stack"]["dies"][die_index]["source_depth_um"] = depth
                with self.subTest(die=die_index, depth=depth), self.assertRaisesRegex(ContractError, "source_depth_um"):
                    validate_contract(bad)
            bad = copy.deepcopy(document)
            del bad["thermal_stack"]["dies"][die_index]["source_depth_um"]
            with self.assertRaisesRegex(ContractError, "source_depth_um"):
                validate_contract(bad)

    def test_power_order_follows_sources_even_when_floorplan_order_changes(self):
        for order in STACK_ORDERS:
            with self.subTest(order=order):
                document = contract(1, 1, thermal_config=thermal_inputs(order))
                document["floorplan"]["elements"].reverse()
                validate_contract(document)
                ordered = power_order_elements(document)
                dram_index = -1 if order == "dram_on_logic" else 0
                self.assertEqual(ordered.pop(dram_index)["die"], "DRAM_DIE")
                self.assertTrue(all(item["die"] == "LOGIC_DIE" for item in ordered))

    def test_generated_sources_face_the_bond_and_manifest_matches_files(self):
        for order in STACK_ORDERS:
            with self.subTest(order=order):
                self.check_generated_stack(order)

    def check_generated_stack(self, order):
        folder = self.generate(contract(1, 1, thermal_config=thermal_inputs(order)))
        text = (folder / "ice.stk").read_text()
        if order == "dram_on_logic":
            expected_order = ["LOGIC_DIE", "DRAM_DIE"]
            self.assertRegex(text, r"die LOGIC_DIE_IC\s*:\s*layer 5 LOGIC_BEOL\s*;\s*source 1 SILICON\s*;\s*layer 49 SILICON")
            self.assertRegex(text, r"die DRAM_DIE_IC\s*:\s*layer 49 DRAM\s*;\s*source 1 DRAM\s*;\s*layer 5 DRAM_BEOL")
        else:
            expected_order = ["DRAM_DIE", "LOGIC_DIE"]
            self.assertRegex(text, r"die LOGIC_DIE_IC\s*:\s*layer 49 SILICON\s*;\s*source 1 SILICON\s*;\s*layer 5 LOGIC_BEOL")
            self.assertRegex(text, r"die DRAM_DIE_IC\s*:\s*layer 5 DRAM_BEOL\s*;\s*source 1 DRAM\s*;\s*layer 49 DRAM")
        stack = text.split("stack:", 1)[1].split("solver:", 1)[0]
        self.assertLess(stack.index(expected_order[1]), stack.index("HYBRID_BOND"))
        self.assertLess(stack.index("HYBRID_BOND"), stack.index(expected_order[0]))
        self.assertNotIn("F2F_BOND", text)
        self.assertRegex(text, r"layer HYBRID_BOND_LAYER\s*:\s*height 2\s*;")
        # Check both anisotropy and SI-to-micrometre conversion through the exporter.
        for name, conductivity, capacity in (
            ("LOGIC_BEOL", (2.2e-6, 2.2e-6, 5e-6 / 2.4), 1.61e-12),
            ("DRAM_BEOL", (2.2e-6, 2.2e-6, 5e-6 / 2.4), 1.61e-12),
            ("HYBRID_BOND", (1.851145038167939e-6, 1.851145038167939e-6, 17e-6), 2.0452e-12),
            ("BUMP", (5.5e-6, 5.5e-6, 113e-6), 1.845e-12),
        ):
            with self.subTest(material=name):
                material = re.search(rf"material {name}\s*:\s*thermal conductivity ([^;]+);\s*"
                                     r"volumetric heat capacity ([^;]+);", text)
                self.assertIsNotNone(material)
                values = [float(value) for value in material[1].split(",")]
                self.assertEqual(len(values), 3)
                for value, expected in zip(values, conductivity):
                    self.assertTrue(math.isclose(value, expected, rel_tol=1e-12), (value, expected))
                self.assertTrue(math.isclose(float(material[2]), capacity, rel_tol=1e-12),
                                (float(material[2]), capacity))
        self.assertRegex(text, r"material DRAM\s*:\s*thermal conductivity 0.00013\s*;\s*volumetric heat capacity 1.61e-12")
        self.assertIn("numofcores 1 ;", text)
        dimensions = re.search(r"chip length ([^,]+), width ([^;]+)", text)
        cells = re.search(r"cell length ([^,]+), width ([^;]+)", text)
        self.assertTrue(all(float(pitch) <= float(size) for pitch, size in zip(cells.groups(), dimensions.groups())),
                        "small dies must retain nonzero passive-layer discretization")
        manifest = json.loads((folder / "thermal_manifest.json").read_text())
        self.assertEqual([die["id"] for die in manifest["dies"]], expected_order)
        names = []
        for die in manifest["dies"]:
            floorplan = (folder / die["floorplan"]).read_text()
            self.assertEqual(re.findall(r"^(\S+)\s*:", floorplan, re.MULTILINE), die["elements"])
            names += die["elements"]
            self.assertIn(die["temperature_output"], text)
            self.assertIn(die["map_output"], text)
        self.assertEqual(names, manifest["power_order"])
        self.assertEqual(names[-1 if order == "dram_on_logic" else 0], "dram__channel_0")

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
        for order in STACK_ORDERS:
            with self.subTest(order=order):
                self.check_native_source_feedback(order)

    def check_native_source_feedback(self, order):
        document = contract(1, 1, thermal_config=thermal_inputs(order))
        folder = self.generate(document)
        manifest = json.loads((folder / "thermal_manifest.json").read_text())
        count = len(manifest["power_order"])
        zeros = [0] * count
        ordered = power_order_elements(document)
        rectangles = [geometry_rectangle(document["geometry"], item["name"]) for item in ordered]
        areas = [width * height for _, _, width, height in rectangles]
        for heated_die, other_die in (("LOGIC_DIE", "DRAM_DIE"), ("DRAM_DIE", "LOGIC_DIE")):
            with self.subTest(heated_die=heated_die):
                heated_area = sum(area for entry, area in zip(ordered, areas) if entry["die"] == heated_die)
                powers = [.02 * area / heated_area if entry["die"] == heated_die else 0
                          for entry, area in zip(ordered, areas)]
                (folder / "power.txt").write_text("\n".join(" ".join(map(str, row)) for row in (zeros, powers, [-1] * count)) + "\n")
                result = subprocess.run([str(SERVER), "ice.stk", "--power-trace", "power.txt", "--until-minus-one"],
                                        cwd=folder, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                temperatures = {}
                for die in manifest["dies"]:
                    rows = [[float(value) for value in line.split()] for line in (folder / die["temperature_output"]).read_text().splitlines()
                            if line.strip() and not line.startswith("%")]
                    self.assertEqual([len(row) for row in rows], [1 + len(die["elements"])] * 2)
                    self.assertTrue(all(abs(value - 300) < .001 for value in rows[0][1:]))
                    self.assertTrue(all(math.isfinite(value) and value >= 300 for value in rows[1][1:]))
                    temperatures[die["id"]] = sum(rows[1][1:]) / len(die["elements"])
                self.assertGreater(temperatures[heated_die], temperatures[other_die])
                self.assertGreater(temperatures[other_die], 300)

    @unittest.skipUnless(SERVER.is_file() and ACTUAL_DRAM3D_ARCH.is_file(), "requires native solver and pinned DRAM3D SDK")
    def test_actual_four_by_four_package_preserves_equilibrium_and_heating_bound(self):
        for order in STACK_ORDERS:
            with self.subTest(order=order):
                self.check_actual_package_heating(order)

    def check_actual_package_heating(self, order):
        # Small meshes did not reveal a parallel-factorization failure seen on
        # the real 4x4 package. Keep its actual layout and passive-layer grid.
        architecture = exporter.import_architecture(ACTUAL_DRAM3D_ARCH)
        document = exporter.build_contract(architecture, ACTUAL_DRAM3D_ARCH, 0,
                                           thermal_config=thermal_inputs(order))
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
    stack_order = "dram_on_logic"
    temporary = MemoryOnLogicTests.temporary
    generate = MemoryOnLogicTests.generate

    def setUp(self):
        self.document = contract(1, 1, thermal_config=thermal_inputs(self.stack_order))
        self.folder = self.generate(self.document)
        self.manifest_path = self.folder / "thermal_manifest.json"
        self.manifest = json.loads(self.manifest_path.read_text())
        self.dies = {die["id"]: die for die in self.manifest["dies"]}
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

    def append_temperatures(self, die_id, value, *, complete=True):
        die = self.dies[die_id]
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
            self.append_temperatures("LOGIC_DIE", 310)
            self.append_temperatures("DRAM_DIE", 330, complete=False)
            time.sleep(.03)
            self.assertFalse(future.done(), "hook returned before the DRAM row was complete")
            with (self.folder / self.dies["DRAM_DIE"]["temperature_output"]).open("a") as stream:
                stream.write("\n")
            self.assertEqual(future.result(timeout=3), 0)
            values = json.loads(Path(update.response).read_text())["temperatures"]
            self.assertTrue(all(abs(item["temperature_c"] - 36.85) < 1e-8 for item in values[:-1]))
            self.assertAlmostEqual(values[-1]["temperature_c"], 56.85)
            final = self.request("final", 10_000_000, 15_000_000)
            future = pool.submit(HOOK.run, final)
            self.wait_power_rows(2)
            self.append_temperatures("DRAM_DIE", 340)
            time.sleep(.03)
            self.assertFalse(future.done(), "hook returned before the logic die advanced")
            self.append_temperatures("LOGIC_DIE", 320)
            self.assertEqual(future.result(timeout=3), 0)
        values = json.loads(Path(final.response).read_text())["temperatures"]
        self.assertAlmostEqual(values[0]["temperature_c"], 46.85)
        self.assertAlmostEqual(values[-1]["temperature_c"], 66.85)
        rows = [[float(value) for value in line.split()] for line in (self.folder / "power.txt").read_text().splitlines()]
        self.assertEqual(rows[0], rows[1])
        logic = [(index + 1) / 100 for index in range(19)] + [0]
        self.assertEqual(rows[0], logic + [.2] if self.stack_order == "dram_on_logic" else [.2] + logic)
        self.assertEqual(rows[2], [-1] * 21)

    def test_mismatched_completed_slots_fail_before_appending_power(self):
        self.append_temperatures("LOGIC_DIE", 310)
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
        logic = list(range(1, 20)) + [0]
        expected = logic + [20] if self.stack_order == "dram_on_logic" else [20] + logic
        self.assertEqual(rows, [expected, [-1] * 21])

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

        self.append_temperatures("LOGIC_DIE", 310)
        self.append_temperatures("DRAM_DIE", 340)
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


class LogicOnDramHookTests(MultiDieHookTests):
    """Run the same coupled transport checks with DRAM first in native order."""
    stack_order = "logic_on_dram"


if __name__ == "__main__":
    unittest.main()
