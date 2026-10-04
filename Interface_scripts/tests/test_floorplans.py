"""Placement-rule invariants, runnable without bootstrapping the SDK."""

import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Interface_scripts"))
from providers.softhier import floorplans
from providers.softhier.floorplans.common import validate_layout, with_residual


def item(name, kind, area):
    return dict(name=name, kind=kind, area_um2=area)


def inventory():
    return [item("redmule", "light_redmule", 100), item("tcdm", "memory", 50),
            item("pe0", "core", 10), item("spatz0", "spatz", 40),
            item("pe1", "core", 10), item("pe2", "core", 10), item("spatz2", "spatz", 40),
            item("idma", "idma", 12), item("data_noc_router", "floonoc", 8),
            item("data_noc_ni", "floonoc", 2), item("instr_mem", "memory", 15),
            item("stack_mem", "memory", 20), item("transpose_engine", "transpose", 3)]


class FloorplanTests(unittest.TestCase):
    def test_both_rules_preserve_area_domains_and_inputs(self):
        items = inventory()
        before = copy.deepcopy(items)
        layouts = []
        for name in floorplans.RULES:
            with self.subTest(rule=name):
                regions, shape = floorplans.build_cluster(items, name)
                validate_layout(with_residual(items), regions, shape)
                self.assertEqual(set(regions), {i["name"] for i in items} | {"others"})
                self.assertEqual(items, before)
                layouts.append((regions, shape))
        self.assertNotEqual(layouts[0][0], layouts[1][0])
        self.assertAlmostEqual(layouts[0][1][0] * layouts[0][1][1],
                               layouts[1][1][0] * layouts[1][1][1])

    def test_default_is_original_redmule_strip(self):
        regions, shape = floorplans.build_cluster(inventory())
        self.assertEqual(floorplans.DEFAULT_RULE, "redmule_strip")
        self.assertEqual(regions["redmule"]["shape"], [10., 10.])
        self.assertEqual(regions["redmule"]["offset"], [0., 0.])
        self.assertEqual(regions["tcdm"]["offset"], [10., 0.])
        self.assertEqual(regions["tcdm"]["shape"][1], shape[1])
        for name, region in regions.items():
            if name not in ("redmule", "tcdm"):
                self.assertGreaterEqual(region["offset"][1], 10.)

    def test_square_three_bands_and_pair_orientation(self):
        items = inventory()
        regions, shape = floorplans.build_cluster(items, "square_bands")
        self.assertEqual(shape[0], shape[1])
        side = shape[0]
        height_a = 150 / side
        height_b = 110 / side
        for name in ("redmule", "tcdm"):
            self.assertAlmostEqual(regions[name]["offset"][1], 0.)
            self.assertAlmostEqual(regions[name]["shape"][1], height_a)
        self.assertEqual(regions["redmule"]["offset"][0], 0.)
        self.assertAlmostEqual(regions["redmule"]["shape"][0], regions["tcdm"]["offset"][0])
        for index in (0, 2):
            pe, vector = regions["pe" + str(index)], regions["spatz" + str(index)]
            self.assertAlmostEqual(pe["offset"][0], vector["offset"][0])
            self.assertAlmostEqual(pe["shape"][0], vector["shape"][0])
            self.assertAlmostEqual(vector["offset"][1], height_a)
            self.assertAlmostEqual(pe["offset"][1], height_a + vector["shape"][1])
            self.assertAlmostEqual(pe["offset"][1] + pe["shape"][1], height_a + height_b)
        self.assertAlmostEqual(regions["pe1"]["offset"][1], height_a)
        self.assertAlmostEqual(regions["pe1"]["shape"][1], height_b)
        self.assertLess(regions["pe0"]["offset"][0], regions["pe1"]["offset"][0])
        self.assertLess(regions["pe1"]["offset"][0], regions["pe2"]["offset"][0])
        support = ["instr_mem", "stack_mem", "idma", "data_noc_router", "data_noc_ni", "transpose_engine", "others"]
        previous_right = 0.
        for name in support:
            region = regions[name]
            self.assertAlmostEqual(region["offset"][1], height_a + height_b)
            self.assertAlmostEqual(region["offset"][0], previous_right)
            self.assertAlmostEqual(region["offset"][1] + region["shape"][1], side)
            previous_right = region["offset"][0] + region["shape"][0]
        self.assertAlmostEqual(previous_right, side)

    def test_no_spatz_and_multiple_dma(self):
        items = [i for i in inventory() if i["kind"] != "spatz" and i["name"] != "idma"]
        items.extend([item("idma_0", "idma", 6), item("idma_1", "idma", 6)])
        for rule in floorplans.RULES:
            regions, shape = floorplans.build_cluster(items, rule)
            self.assertIn("idma_0", regions)
            self.assertIn("idma_1", regions)
            self.assertFalse(any(name.startswith("spatz") for name in regions))
        self.assertEqual(regions["pe0"]["offset"][1], regions["pe2"]["offset"][1])
        self.assertEqual(regions["pe0"]["shape"][1], regions["pe2"]["shape"][1])

    def test_core_order_is_numeric_not_lexical(self):
        items = inventory() + [item("pe10", "core", 10)]
        regions, _ = floorplans.build_cluster(list(reversed(items)), "square_bands")
        self.assertLess(regions["pe2"]["offset"][0], regions["pe10"]["offset"][0])

    def test_unknown_rule_and_orphan_spatz_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown SoftHier floorplan"):
            floorplans.build_cluster(inventory(), "missing")
        with self.assertRaisesRegex(ValueError, "matching"):
            floorplans.build_cluster(inventory() + [item("spatz9", "spatz", 40)], "square_bands")

    def test_bad_areas_and_duplicate_names_are_rejected(self):
        for area in (0., -1., float("nan"), float("inf")):
            items = inventory()
            items[0]["area_um2"] = area
            with self.subTest(area=area), self.assertRaisesRegex(ValueError, "finite and positive"):
                floorplans.build_cluster(items)
        with self.assertRaisesRegex(ValueError, "unique"):
            floorplans.build_cluster(inventory() + [inventory()[0]])

    def test_validator_rejects_wrong_area_overlap_and_missing_region(self):
        items = with_residual(inventory())
        regions, shape = floorplans.build_cluster(inventory(), "square_bands")
        bad = copy.deepcopy(regions)
        bad["tcdm"]["offset"][0] = 0.
        with self.assertRaisesRegex(ValueError, "overlapping"):
            validate_layout(items, bad, shape)
        bad = copy.deepcopy(regions)
        bad["redmule"]["shape"][0] *= .9
        with self.assertRaisesRegex(ValueError, "changed component area"):
            validate_layout(items, bad, shape)
        bad = copy.deepcopy(regions)
        del bad["others"]
        with self.assertRaisesRegex(ValueError, "exactly once"):
            validate_layout(items, bad, shape)

    def test_provider_propagates_choice_without_sdk(self):
        # A minimal architecture is sufficient to test real shell/CLI wiring.
        source = """class FlexClusterArch:
    num_cluster_x = 2
    num_cluster_y = 1
    num_core_per_cluster = 1
    redmule_ce_height = 2
    redmule_ce_width = 2
    cluster_tcdm_size = 1024
    noc_link_width = 64
    idma_outstand_txn = 2
    instruction_mem_size = 128
    cluster_stack_size = 256
    cluster_tcdm_bank_width = 32
    cluster_tcdm_bank_nb = 4
"""
        import json
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            arch, output = folder / "arch.py", folder / "system.json"
            arch.write_text(source)
            env = dict(os.environ, SIMULATOR_CONFIG=str(arch), SYSTEM_CONFIG_FILE=str(output),
                       SOFTHIER_FLOORPLAN="square_bands", PYTHON=sys.executable)
            subprocess.run(["bash", str(ROOT / "Interface_scripts/providers/softhier/provider.sh"), "export-system"],
                           env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            contract = json.loads(output.read_text())
            self.assertEqual(contract["metadata"]["floorplan_rule"], "square_bands")
            shape = contract["geometry"]["chip"]["subs"]["cluster_0"]["shape"]
            self.assertAlmostEqual(shape[0], shape[1])


if __name__ == "__main__":
    unittest.main()
