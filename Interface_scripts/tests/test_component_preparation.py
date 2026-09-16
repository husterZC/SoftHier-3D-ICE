"""Regression tests for out-of-tree preparation and microsecond map times."""

import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


STAGE = load("runtime_staging", "Interface_scripts/providers/softhier/prepare_workload.py")
PREP = load("kernel_preparation", "experiments/component_power/prepare_kernel.py")
VIEW = load("temperature_view", "Interface_scripts/plot_runtime_temperature_map/plot_runtime_tmap.py")
SDK = ROOT / "SoftHier/soft_hier_sdk"


class PreparationTests(unittest.TestCase):
    @unittest.skipUnless((SDK / "implementation/config/arch/arch.py").is_file(),
                         "requires bootstrapped SoftHier SDK")
    def test_runtime_staging_preserves_sdk_headers(self):
        headers = [SDK / "runtime/runtime/include" / f"flex_cluster_arch.{ext}" for ext in ("h", "inc")]
        before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in headers]
        with tempfile.TemporaryDirectory() as temporary:
            runtime, isa = STAGE.stage(SDK, Path(temporary), SDK / "implementation/config/arch/arch.py")
            self.assertEqual(isa, "rv32imafdv_zfh")
            self.assertIn("ARCH_NUM_CORE_PER_CLUSTER 5", (runtime / "runtime/include/flex_cluster_arch.h").read_text())
        self.assertEqual(before, [hashlib.sha256(path.read_bytes()).hexdigest() for path in headers])

    @unittest.skipUnless((SDK / "implementation/sw").is_dir(),
                         "requires bootstrapped SoftHier SDK")
    def test_repeated_kernels_reinitialize_execution_region(self):
        def generator(command, **kwargs):
            if command[1].endswith("_preload.py"):
                Path(command[2]).write_bytes(b"test-elf")
            self.assertIn("cwd", kwargs)  # Upstream generators use fixed scratch filenames.
        for kernel in PREP.KERNELS:
            with self.subTest(kernel=kernel), tempfile.TemporaryDirectory() as temporary, patch.object(PREP.subprocess, "run", side_effect=generator):
                result = PREP.prepare(SDK, kernel, Path(temporary), SDK / "implementation/config/arch/arch.py",
                                      SDK / f"implementation/config/kernels/{kernel}.py", repeats=3)
                source = (Path(result["app"]) / "main.c").read_text()
                self.assertIn("study_repeat < 3", source)
                entry = "flat_attention(" if kernel == "attn" else "Info info ="
                self.assertLess(source.index("study_repeat < 3"), source.index(entry))
                self.assertEqual(result["repeats"], 3)

    def test_microsecond_floorplan_timestamps(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "temperatures.txt"
            path.write_text("% Time(s) test(K)\n0.000 300.001\n0.000 300.002\n")
            names, rows = VIEW.load_tflp_rows(path, slot_seconds=1e-6)
            self.assertEqual(names, ["test"])
            self.assertEqual([row["time"] for row in rows], [1e-6, 2e-6])
            self.assertEqual(rows[1]["values"], [300.002])
            for bad in (0, -1, float("nan")):
                with self.assertRaisesRegex(ValueError, "positive"):
                    VIEW.load_tflp_rows(path, slot_seconds=bad)
