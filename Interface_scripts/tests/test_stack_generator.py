"""Default package geometry and power/temperature integration regressions."""

import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "Interface_scripts/geometry_generator/roi2ice_stk.py"
SERVER = ROOT / "3D-ICE/bin/3D-ICE-Server"


def stack_elements(contents):
    section = contents.split("stack:", 1)[1].split("solver:", 1)[0]
    return re.findall(
        r'^\s*(layer|die)\s+(\w+)\s+(\w+)(?:\s+floorplan\s+"([^"]+)")?\s*;',
        section,
        re.MULTILINE,
    )


def numeric_rows(path):
    return [
        list(map(float, line.split()))
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("%")
    ]


class StackGeneratorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="stack-generator-tests-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def generate(self, length=200, width=100, slot_seconds=1e-4, extra_args=()):
        directory = self.directory / f"{length}x{width}"
        floorplan = directory / "floorplans/source.flp"
        stack = directory / "generated/ice.stk"
        floorplan.parent.mkdir(parents=True)
        # Two equal-area power domains make ordering and heat response observable.
        floorplan.write_text(
            f"""left :
 position 0, 0;
 dimension {length / 2}, {width};
 discretization 2, 2;
right :
 position {length / 2}, 0;
 dimension {length / 2}, {width};
 discretization 2, 2;
"""
        )
        subprocess.run(
            [sys.executable, str(GENERATOR), str(floorplan), str(stack),
             "--slot-seconds", str(slot_seconds), *extra_args],
            check=True, capture_output=True, text=True, timeout=10,
        )
        return stack, floorplan

    def test_reference_stack_materials_and_layers_with_only_one_powered_die(self):
        stack, _ = self.generate()
        contents = stack.read_text()
        elements = stack_elements(contents)
        passive = {
            name: (material, float(height))
            for name, height, material in re.findall(
                r"^layer\s+(\w+)\s*:\s*height\s+(\S+)\s*;\s*material\s+(\w+)\s*;",
                contents, re.MULTILINE,
            )
        }
        active = {
            name: (material, float(height))
            for name, height, material in re.findall(
                r"^die\s+(\w+)\s*:\s*source\s+(\S+)\s+(\w+)\s*;",
                contents, re.MULTILINE,
            )
        }
        physical_layers = [
            (active if kind == "die" else passive)[definition]
            for kind, _, definition, _ in elements
        ]
        self.assertEqual(
            physical_layers,
            [("CU", 50)] + [("TIM", 7.5)] * 4
            + [("SILICON", 50), ("BUMP", 50)] + [("SILICON", 250)] * 4,
        )
        self.assertEqual(sum(height for _, height in physical_layers), 1180)
        self.assertEqual(
            [name for _, name, _, _ in elements],
            ["HEATSINK", "TIM_1", "TIM_2", "TIM_3", "TIM_4", "TOP_DIE",
             "BUMP", "PCB_1", "PCB_2", "PCB_3", "PCB_4"],
        )
        self.assertEqual([name for kind, name, _, _ in elements if kind == "die"], ["TOP_DIE"])
        self.assertEqual(sum(bool(path) for _, _, _, path in elements), 1)

        materials = {
            name: (tuple(map(float, conductivity.split(","))), float(capacity))
            for name, conductivity, capacity in re.findall(
                r"^material\s+(\w+)\s*:\s*thermal conductivity\s+([^;]+);"
                r"\s*volumetric heat capacity\s+(\S+)\s*;",
                contents, re.MULTILINE,
            )
        }
        self.assertEqual(materials, {
            "SILICON": ((1.3e-4,), 1.61e-12),
            "CU": ((3.85e-4,), 3.45e-12),
            "TIM": ((5e-6,), 2.5e-12),
            "BUMP": ((5.5e-6, 5.5e-6, 1.13e-4), 1.845e-12),
        })
        coefficient = re.search(r"heat transfer coefficient\s+(\S+)\s*;", contents)
        self.assertEqual(float(coefficient[1]), 1.5e-8)

    def test_footprint_and_floorplan_path_follow_the_input(self):
        for length, width in [(200, 200), (300, 100), (333.5, 177.25)]:
            with self.subTest(length=length, width=width):
                stack, floorplan = self.generate(length, width)
                contents = stack.read_text()
                dimensions = re.search(r"chip length\s+([^,]+),\s*width\s+([^;]+);", contents)
                self.assertEqual(tuple(map(float, dimensions.groups())), (length, width))
                path = next(path for kind, _, _, path in stack_elements(contents) if kind == "die")
                self.assertFalse(Path(path).is_absolute())
                self.assertEqual((stack.parent / path).resolve(), floorplan)

    def test_solver_timing_and_temperature_settings_are_preserved(self):
        for extra_args, expected_step, temperature in [
            ((), 1e-5, 300),
            (("--step-seconds", "0.000025", "--initial-temperature-k", "315"), 2.5e-5, 315),
        ]:
            with self.subTest(extra_args=extra_args):
                stack, _ = self.generate(width=temperature, extra_args=extra_args)
                contents = stack.read_text()
                timing = re.search(r"transient step\s+([^,]+),\s*slot\s+([^;]+);", contents)
                self.assertEqual(tuple(map(float, timing.groups())), (expected_step, 1e-4))
                temperatures = re.findall(r"\btemperature\s+(\S+)\s*;", contents)
                self.assertEqual(list(map(float, temperatures)), [temperature, temperature])

    @unittest.skipUnless(SERVER.is_file(), "requires the built 3D-ICE server")
    def test_native_solver_preserves_alignment_power_order_and_feedback(self):
        for length, width in [(200, 200), (300, 100), (333.5, 177.25)]:
            with self.subTest(length=length, width=width):
                # Tflp timestamps have millisecond precision in the native output.
                stack, _ = self.generate(length, width, slot_seconds=0.001)
                contents = stack.read_text()
                elements = stack_elements(contents)
                # Diagnostic outputs let the native solver expose passive-layer bounds.
                contents += "".join(
                    f'Tmap({name}, "{name}.txt", slot);\n'
                    for kind, name, _, _ in elements if kind == "layer"
                )
                stack.write_text(contents)
                (stack.parent / "power.txt").write_text("0 0\n0.02 0.001\n-1 -1\n")
                result = subprocess.run(
                    [str(SERVER), stack.name, "--power-trace", "power.txt", "--until-minus-one"],
                    cwd=stack.parent, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("termination slot at slot 2", result.stdout)

                averages = numeric_rows(stack.parent / "output_top_die_flp_avg.txt")
                maps = numeric_rows(stack.parent / "output_top_die.txt")
                self.assertEqual([len(row) for row in averages], [3, 3])
                self.assertEqual([len(row) for row in maps], [8, 8])
                for row, timestamp in zip(averages, [0.001, 0.002]):
                    self.assertAlmostEqual(row[0], timestamp)
                for value in averages[0][1:] + maps[0]:
                    self.assertAlmostEqual(value, 300, places=3)
                self.assertTrue(all(math.isfinite(value) and value >= 300 for value in maps[1]))
                self.assertGreater(averages[1][1], averages[1][2])
                self.assertGreater(averages[1][2], 300)
                # Feedback and maps must both refer to the embedded source layer.
                for row, average in zip(maps, averages):
                    for column, offset in [(1, 0), (2, 4)]:
                        self.assertAlmostEqual(
                            average[column], sum(row[offset:offset + 4]) / 4,
                            delta=0.001,
                        )

                for kind, name, _, _ in elements:
                    coordinates = numeric_rows(stack.parent / f"xyaxis_{name}.txt")
                    self.assertTrue(coordinates, name)
                    self.assertEqual(min(row[0] for row in coordinates), 0)
                    self.assertEqual(min(row[1] for row in coordinates), 0)
                    self.assertAlmostEqual(max(x + dx for x, y, dx, dy in coordinates), length, delta=0.02)
                    self.assertAlmostEqual(max(y + dy for x, y, dx, dy in coordinates), width, delta=0.02)
                    self.assertTrue(all(dx > 0 and dy > 0 for x, y, dx, dy in coordinates))
                    self.assertAlmostEqual(
                        sum(dx * dy for x, y, dx, dy in coordinates),
                        length * width, delta=length * width * 5e-4,
                    )
                    temperatures = maps if kind == "die" else numeric_rows(stack.parent / f"{name}.txt")
                    self.assertEqual([len(row) for row in temperatures], [len(coordinates)] * 2)
                    for value in temperatures[0]:
                        self.assertAlmostEqual(value, 300, places=3)
                    self.assertTrue(all(math.isfinite(value) and value >= 300 for value in temperatures[1]))


if __name__ == "__main__":
    unittest.main()
