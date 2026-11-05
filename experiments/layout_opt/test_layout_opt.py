"""Scientific invariants for cluster symmetries and the provider boundary."""

import copy
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "Interface_scripts"))
from providers.softhier import floorplans
from providers.softhier.export_system_config import build_contract, import_architecture
from providers.softhier.floorplans.common import validate_layout, with_residual
from experiments.layout_opt.transforms import (SYMMETRIES, local, plan_for,
    transform_contract, transform_regions, validate_plan, write_json)
from experiments.layout_opt.native_export import native_contract

ARCH = ROOT / "SoftHier/soft_hier_sdk/apps_dram3d/config/arch.py"


class SymmetryTests(unittest.TestCase):
    def setUp(self):
        self.items = [dict(name=n, kind=k, area_um2=a) for n, k, a in (
            ("redmule", "light_redmule", 37), ("tcdm", "memory", 53),
            ("pe0", "core", 3), ("spatz0", "spatz", 8),
            ("instr_mem", "memory", 7), ("stack_mem", "memory", 6), ("idma", "idma", 2))]
        self.regions, self.shape = floorplans.build_cluster(self.items, "square_bands")

    def test_eight_distinct_rigid_layouts_preserve_all_areas_and_coverage(self):
        before = copy.deepcopy(self.regions)
        signatures = set()
        for name in SYMMETRIES:
            result = transform_regions(self.regions, self.shape, name)
            validate_layout(with_residual(self.items), result, self.shape)
            signatures.add(json.dumps(result, sort_keys=True))
            # A single rigid transform preserves distances between domain centers.
            centers = lambda rs: {n: [r["offset"][i] + r["shape"][i]/2 for i in (0, 1)]
                                  for n, r in rs.items()}
            original, changed = centers(before), centers(result)
            for a in original:
                for b in original:
                    self.assertAlmostEqual(math.dist(original[a], original[b]),
                                           math.dist(changed[a], changed[b]))
        self.assertEqual(len(signatures), 8)
        self.assertEqual(self.regions, before)
        self.assertEqual(transform_regions(before, self.shape, "R0"), before)

    def test_known_corners_and_rotation_direction(self):
        rectangle = {"a": dict(type="comp", shape=[2., 3.], offset=[0., 0.], subs={}),
                     "b": dict(type="comp", shape=[8., 3.], offset=[2., 0.], subs={}),
                     "c": dict(type="comp", shape=[10., 7.], offset=[0., 3.], subs={})}
        expected = {"R0": [0, 0], "R90": [7, 0], "R180": [8, 7], "R270": [0, 8],
                    "MX": [8, 0], "MY": [0, 7], "MD": [0, 0], "MA": [7, 8]}
        for name, offset in expected.items():
            result = transform_regions(rectangle, [10., 10.], name)["a"]
            self.assertEqual(result["offset"], offset, name)
            self.assertEqual(result["shape"], [3., 2.] if name in ("R90", "R270", "MD", "MA") else [2., 3.])

    def test_inverse_transform_recovers_original(self):
        for name in SYMMETRIES:
            inverse = {"R90": "R270", "R270": "R90"}.get(name, name)
            result = transform_regions(transform_regions(self.regions, self.shape, name), self.shape, inverse)
            for component in result:
                for field in ("offset", "shape"):
                    for left, right in zip(result[component][field], self.regions[component][field]):
                        self.assertAlmostEqual(left, right)

    def test_rejects_incomplete_unknown_and_wrong_grid_plans(self):
        good = plan_for(["R0"]*16)
        for change in (dict(base_rule="redmule_strip"), dict(columns=2),
                       dict(transforms={}), dict(schema_version=True)):
            with self.assertRaises(ValueError):
                validate_plan(dict(good, **change), dict(columns=4, rows=4))
        bad = copy.deepcopy(good)
        bad["transforms"]["cluster_0"] = "scale"
        with self.assertRaises(ValueError):
            validate_plan(bad, dict(columns=4, rows=4))
        with self.assertRaises(ValueError):
            transform_regions(self.regions, [10, 20], "R90")

    def test_rejects_paths_outside_repo_before_reading(self):
        with self.assertRaises(ValueError):
            local(ROOT.parent / "forbidden-layout.json")


@unittest.skipUnless(ARCH.is_file(), "requires the pinned SDK")
class ProviderTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / "tmp/layout_opt_tests"
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.contract = build_contract(import_architecture(ARCH), ARCH, 0., floorplan_rule="square_bands")

    def test_contract_changes_only_local_logic_geometry_and_layout_metadata(self):
        original = copy.deepcopy(self.contract)
        plan = plan_for(list(SYMMETRIES)*2)
        result = transform_contract(original, plan)
        self.assertEqual(original, self.contract)
        for field in ("thermal_stack", "floorplan", "power_trace", "thermal_feedback"):
            self.assertEqual(result[field], original[field])
        self.assertEqual(result["geometry"]["dram"], original["geometry"]["dram"])
        for name, cluster in result["geometry"]["chip"]["subs"].items():
            old = original["geometry"]["chip"]["subs"][name]
            self.assertEqual(cluster["shape"], old["shape"])
            self.assertEqual(cluster["offset"], old["offset"])
            self.assertEqual(set(cluster["subs"]), set(old["subs"]))
        with self.assertRaises(ValueError):
            transform_contract(result, plan)

    def test_real_provider_transports_independent_cluster_transforms(self):
        layout, output = self.folder / "layout.json", self.folder / "system.json"
        plan = plan_for(["MX", "R90"] + ["R0"]*14)
        write_json(layout, plan)
        env = dict(os.environ, ROOT_DIR=str(ROOT), PYTHON=sys.executable,
            SIMULATOR_CONFIG=str(ARCH), SYSTEM_CONFIG_FILE=str(output),
            SOFTHIER_FLOORPLAN="square_bands", LAYOUT_OPT_CONFIG=str(layout))
        subprocess.run([str(ROOT / "experiments/layout_opt/provider.sh"), "export-system"],
                       env=env, capture_output=True, text=True, check=True)
        result = json.loads(output.read_text())
        expected = native_contract(self.contract, plan)
        self.assertEqual(result["geometry"], expected["geometry"])
        self.assertEqual(result["metadata"]["layout_optimization"]["transforms"], plan["transforms"])

    @unittest.skipUnless((ROOT / "3D-ICE/bin/3D-ICE-Server").is_file(), "requires native solver")
    def test_all_symmetries_parse_and_remain_at_ambient_with_zero_power(self):
        contract = native_contract(self.contract, plan_for(list(SYMMETRIES)*2))
        self.assertEqual(native_contract(self.contract, plan_for(["R0"]*16))["geometry"], self.contract["geometry"])
        source = self.folder / "system.json"
        write_json(source, contract)
        generated = self.folder / "generated"
        generated.mkdir()
        subprocess.run([sys.executable, str(ROOT / "Interface_scripts/geometry_generator/generate_3dice_inputs.py"),
            "--system-config", str(source), "--geo", str(self.folder / "geo.json"),
            "--floorplan", str(generated / "floorplan.flp"), "--stk", str(generated / "ice.stk"),
            "--target-top-die-cells", "256", "--power-interval-ps", "20000000"],
            capture_output=True, text=True, check=True)
        manifest = json.loads((generated / "thermal_manifest.json").read_text())
        width = len(manifest["power_order"])
        (generated / "power.txt").write_text(" ".join(["0"]*width)+"\n"+" ".join(["-1"]*width)+"\n")
        subprocess.run([str(ROOT / "3D-ICE/bin/3D-ICE-Server"), "ice.stk", "--power-trace", "power.txt", "--until-minus-one"],
            cwd=generated, capture_output=True, text=True, check=True, timeout=60)
        for die in manifest["dies"]:
            rows = [line for line in (generated / die["map_output"]).read_text().splitlines()
                    if line.strip() and not line.startswith("%")]
            self.assertEqual(len(rows), 1)
            for value in map(float, rows[0].split()):
                self.assertAlmostEqual(value, 300., places=3)


if __name__ == "__main__":
    unittest.main()
