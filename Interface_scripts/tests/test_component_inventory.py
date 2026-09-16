"""Architecture-derived areas and power/thermal mappings, without GVSoC."""

import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("component_exporter",
    ROOT / "Interface_scripts/providers/softhier/export_system_config.py")
EXPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPORT)
ARCH = ROOT / "SoftHier/soft_hier_sdk/implementation/config/arch/arch.py"


@unittest.skipUnless(ARCH.is_file(), "requires bootstrapped SoftHier SDK")
class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.arch = EXPORT.import_architecture(ARCH)

    def test_implementation_architecture(self):
        contract = EXPORT.build_contract(self.arch, ARCH, 0, "temperature_aware")
        self.assertEqual(len(contract["power_trace"]["columns"]), 304)
        self.assertEqual(len(contract["floorplan"]["elements"]), 320)
        self.assertEqual(contract["metadata"]["power_voltage_v"], 0.7)
        inventory = EXPORT.component_inventory(self.arch)
        vectors = [i for i in inventory if i["kind"] == "spatz"]
        self.assertEqual(len(vectors), 4)
        self.assertEqual(vectors[0]["parameters"]["vrf_bytes"], 1024)
        self.assertEqual(vectors[0]["parameters"]["function_units"], 4)
        transpose = next(i for i in inventory if i["kind"] == "transpose")
        self.assertEqual(transpose["parameters"]["buffer_bytes"], 512)

    def test_layout_area_conservation_and_disjoint_rectangles(self):
        regions, shape = EXPORT.build_cluster_geometry(self.arch)
        inventory = EXPORT.component_inventory(self.arch)
        for item in inventory:
            rect = regions[item["name"]]
            self.assertAlmostEqual(rect["shape"][0] * rect["shape"][1], item["area_um2"])
        self.assertAlmostEqual(sum(r["shape"][0] * r["shape"][1] for r in regions.values()), shape[0] * shape[1])
        for index, (name, rect) in enumerate(regions.items()):
            x, y = rect["offset"]
            w, h = rect["shape"]
            self.assertGreater(w, 0)
            self.assertGreater(h, 0)
            self.assertLessEqual(x + w, shape[0] + 1e-8)
            self.assertLessEqual(y + h, shape[1] + 1e-8)
            for other_name, other in list(regions.items())[index + 1:]:
                ox, oy = other["offset"]
                ow, oh = other["shape"]
                intersection = max(0, min(x+w, ox+ow)-max(x, ox)) * max(0, min(y+h, oy+oh)-max(y, oy))
                self.assertLess(intersection, 1e-7, (name, other_name))

    def test_no_vector_and_multiple_dma_variants(self):
        self.arch.spatz_attaced_core_list = []
        self.arch.multi_idma_enable = 1
        inventory = EXPORT.component_inventory(self.arch)
        self.assertFalse(any(i["kind"] == "spatz" for i in inventory))
        self.assertEqual(len([i for i in inventory if i["kind"] == "idma"]), 5)
        for rule in EXPORT.floorplans.RULES:
            EXPORT.build_contract(self.arch, ARCH, 0, floorplan_rule=rule)

    def test_layout_selection_leaves_power_and_feedback_mappings_unchanged(self):
        original = EXPORT.build_contract(self.arch, ARCH, 0)
        square = EXPORT.build_contract(self.arch, ARCH, 0, floorplan_rule="square_bands")
        self.assertNotEqual(original["geometry"], square["geometry"])
        self.assertEqual(original["metadata"]["floorplan_rule"], "redmule_strip")
        self.assertEqual(square["metadata"]["floorplan_rule"], "square_bands")
        for field in ("power_trace", "thermal_feedback", "floorplan"):
            self.assertEqual(original[field], square[field])
        for field in ("components", "power_model_specs", "power_voltage_v", "power_frequency_hz"):
            self.assertEqual(original["metadata"][field], square["metadata"][field])
        self.assertEqual(len(square["power_trace"]["columns"]), 304)
        self.assertEqual(len(square["floorplan"]["elements"]), 320)
        regions, shape = EXPORT.build_cluster_geometry(self.arch, "square_bands")
        self.assertEqual(shape[0], shape[1])

    def test_reject_duplicate_spatz_attachment(self):
        self.arch.spatz_attaced_core_list = [0, 0]
        with self.assertRaisesRegex(ValueError, "unique"):
            EXPORT.component_inventory(self.arch)
