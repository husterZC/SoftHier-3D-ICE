#!/usr/bin/env python3

import importlib.util
import math
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
MODEL_PATH = (
    ROOT_DIR
    / "SoftHier"
    / "pulp"
    / "pulp"
    / "chips"
    / "soft_hier_old"
    / "power_models"
    / "__init__.py"
)
MODEL_SPEC = importlib.util.spec_from_file_location(
    "softhier_component_power_models", str(MODEL_PATH)
)
MODELS = importlib.util.module_from_spec(MODEL_SPEC)
MODEL_SPEC.loader.exec_module(MODELS)


def table_value(table, temperature, voltage):
    voltages = table["values"][str(temperature)]
    return next(value["any"] for key, value in voltages.items() if float(key) == float(voltage))


class ComponentPowerModelTests(unittest.TestCase):
    def test_new_logic_profiles_and_estimate_sensitivity(self):
        for component, parameters in (
            ("core", {}),
            ("spatz", dict(function_units=4, vrf_bytes=1024, vlsu_ports=8)),
            ("idma", dict(outstanding=64, data_width_bits=1024)),
            ("floonoc", dict(part="router", data_width_bits=1024)),
            ("floonoc", dict(part="ni", data_width_bits=32)),
            ("transpose", dict(buffer_bytes=4096)),
        ):
            for node in ("22nm", "12nm", "7nm", "5nm"):
                with self.subTest(component=component, node=node, parameters=parameters):
                    kwargs = dict(tech_node=node, **parameters)
                    constant = MODELS.logic_power_sources(component, profile="constant", **kwargs)
                    aware = MODELS.logic_power_sources(component, profile="temperature_aware", **kwargs)
                    double = MODELS.logic_power_sources(component, profile="constant", estimate_scale=2, **kwargs)
                    voltage = MODELS.technology_spec(node)["nominal_voltage_v"]
                    for name, source in constant.items():
                        self.assertEqual(source["dynamic"], aware[name]["dynamic"])
                        energy = table_value(source["dynamic"], 25, voltage)
                        self.assertGreaterEqual(energy, 0)
                        self.assertAlmostEqual(table_value(double[name]["dynamic"], 25, voltage), 2 * energy)
                    leakage = constant["background"]["leakage"]
                    self.assertAlmostEqual(table_value(leakage, 25, voltage), table_value(leakage, 125, voltage))
                    self.assertAlmostEqual(table_value(leakage, 25, voltage), table_value(aware["background"]["leakage"], 25, voltage))
                    self.assertGreater(table_value(aware["background"]["leakage"], 125, voltage), table_value(leakage, 25, voltage))

    def test_paper_reference_units(self):
        spatz = MODELS.logic_power_sources("spatz", tech_node="12nm", profile="constant",
            function_units=4, vrf_bytes=2048, vlsu_ports=4)
        self.assertAlmostEqual(table_value(spatz["fma_64"]["dynamic"], 25, 0.8), 18.1)
        self.assertAlmostEqual(table_value(spatz["vrf_byte"]["dynamic"], 25, 0.8) * 24, 5, places=7)
        # Per-byte router energy is not multiplied by data-path width again.
        for width in (512, 1024):
            noc = MODELS.logic_power_sources("floonoc", tech_node="12nm", profile="constant",
                part="router", data_width_bits=width)
            self.assertAlmostEqual(table_value(noc["payload_byte"]["dynamic"], 25, 0.8) * 4096, 614.4)

    def test_background_and_events_frequency_units(self):
        one = MODELS.logic_power_sources("core", tech_node="12nm", profile="constant")
        two = MODELS.logic_power_sources("core", tech_node="12nm", profile="constant", frequency_hz=2e9)
        for name in one:
            ratio = 2 if name == "background" else 1
            self.assertAlmostEqual(table_value(two[name]["dynamic"], 25, 0.8),
                ratio * table_value(one[name]["dynamic"], 25, 0.8))
        self.assertEqual(one["background"]["leakage"], two["background"]["leakage"])

    def test_instruction_groups(self):
        for label, group in {"add": 0, "c.lw": 1, "flh": 1, "bne": 2,
            "mul": 3, "divu": 4, "fmadd.h": 5, "vfmul.vv": 6, "wfi": 7}.items():
            self.assertEqual(MODELS.core_instruction_group(label), group, label)

    def test_invalid_logic_dimensions(self):
        for width in (-1, 0, 2.5, float("nan"), float("inf")):
            with self.assertRaisesRegex(ValueError, "positive integer"):
                MODELS.logic_area_kge("floonoc", part="router", data_width_bits=width)

    def test_dynamic_tables_reproduce_original_values(self):
        macs = 4_194_304
        size = 1_049_216
        original = {
            "22nm": {
                "redmule_dynamic": {0.6: 1.4, 1.0: 4.0},
                "memory_dynamic": {0.6: 0.29, 1.0: 0.80},
            },
            "12nm": {
                "redmule_dynamic": {0.6: 1.1, 0.9: 2.4},
                "memory_dynamic": {0.6: 0.28, 0.9: 0.48},
            },
            "7nm": {
                "redmule_dynamic": {0.6: 0.9, 0.8: 1.6},
                "memory_dynamic": {0.6: 0.25, 0.8: 0.32},
            },
            "5nm": {
                "redmule_dynamic": {0.6: 0.8, 0.7: 1.2},
                "memory_dynamic": {0.6: 0.15, 0.7: 0.20},
            },
        }

        for node, expected in original.items():
            with self.subTest(node=node):
                redmule = MODELS.light_redmule_power_source(
                    num_tile_mac=macs,
                    redmule_kge=35_284.64,
                    tech_node=node,
                    profile="constant",
                )
                memory = MODELS.memory_power_sources(
                    size_bytes=size, tech_node=node, profile="constant"
                )

                self.assertIn("leakage", redmule)
                for voltage, value in expected["redmule_dynamic"].items():
                    self.assertAlmostEqual(
                        table_value(redmule["dynamic"], 25, voltage), value * macs
                    )
                for voltage, value in expected["memory_dynamic"].items():
                    self.assertAlmostEqual(
                        table_value(
                            memory["access_byte"]["dynamic"], 25, voltage
                        ),
                        value,
                    )

    def test_constant_is_temperature_invariant_control(self):
        kwargs = dict(
            num_tile_mac=4_194_304,
            redmule_kge=35_284.64,
            tech_node="5nm",
            profile="constant",
        )
        redmule = MODELS.light_redmule_power_source(**kwargs)
        leakage = redmule["leakage"]
        self.assertAlmostEqual(
            table_value(leakage, 25, 0.7), table_value(leakage, 125, 0.7)
        )

        memory = MODELS.memory_power_sources(
            size_bytes=MODELS.MIB, tech_node="5nm", profile="constant"
        )
        leakage = memory["background"]["leakage"]
        self.assertAlmostEqual(table_value(leakage, 25, 0.7), 0.020)
        self.assertAlmostEqual(
            table_value(leakage, 25, 0.7), table_value(leakage, 125, 0.7)
        )

    def test_temperature_aware_profile_is_exponential_and_monotonic(self):
        redmule = MODELS.light_redmule_power_source(
            num_tile_mac=4_194_304,
            redmule_kge=35_284.64,
            tech_node="5nm",
            profile="temperature_aware",
        )
        leakage = redmule["leakage"]
        values = [
            table_value(leakage, int(temp), 0.7)
            for temp in MODELS.TEMPERATURE_GRID_C
        ]
        self.assertTrue(all(b > a for a, b in zip(values, values[1:])))
        expected_125 = values[0] * math.pow(2.0, 100.0 / 22.0)
        self.assertAlmostEqual(values[-1], expected_125)

    def test_control_and_temperature_aware_share_reference_leakage(self):
        for component in ("light_redmule", "memory"):
            if component == "light_redmule":
                build = lambda profile: MODELS.light_redmule_power_source(
                    num_tile_mac=4_194_304,
                    redmule_kge=35_284.64,
                    tech_node="5nm",
                    profile=profile,
                )["leakage"]
            else:
                build = lambda profile: MODELS.memory_power_sources(
                    size_bytes=MODELS.MIB,
                    tech_node="5nm",
                    profile=profile,
                )["background"]["leakage"]

            self.assertAlmostEqual(
                table_value(build("constant"), 25, 0.7),
                table_value(build("temperature_aware"), 25, 0.7),
                msg=component,
            )

    def test_normalized_leakage_scales_with_design_size(self):
        small = MODELS.light_redmule_power_source(
            num_tile_mac=1,
            redmule_kge=10_000,
            tech_node="7nm",
            profile="constant",
        )
        large = MODELS.light_redmule_power_source(
            num_tile_mac=1,
            redmule_kge=20_000,
            tech_node="7nm",
            profile="constant",
        )
        self.assertAlmostEqual(
            table_value(large["leakage"], 25, 0.8),
            2.0 * table_value(small["leakage"], 25, 0.8),
        )

        small_mem = MODELS.memory_power_sources(
            size_bytes=MODELS.MIB, tech_node="7nm", profile="constant"
        )
        large_mem = MODELS.memory_power_sources(
            size_bytes=2 * MODELS.MIB, tech_node="7nm", profile="constant"
        )
        self.assertAlmostEqual(
            table_value(large_mem["background"]["leakage"], 25, 0.8),
            2.0
            * table_value(small_mem["background"]["leakage"], 25, 0.8),
        )

    def test_logic_leakage_density_rises_toward_advanced_nodes(self):
        # The same kGE occupies less area at newer nodes. These area-per-kGE
        # assumptions match export_system_config.py, including 66% utilization.
        kge_to_um2 = {"22nm": 200, "12nm": 120, "7nm": 60, "5nm": 30}
        densities = []
        for node in ("22nm", "12nm", "7nm", "5nm"):
            data = MODELS.component_model_metadata("light_redmule", node)
            nominal = max(data["leakage_uw_per_kge_at_25c"].values())
            area_um2_per_kge = kge_to_um2[node] / 0.66
            densities.append(nominal / area_um2_per_kge)
        self.assertTrue(all(b > a for a, b in zip(densities, densities[1:])))

    def test_removed_legacy_profile_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown SoftHier power profile"):
            MODELS.validate_power_profile("legacy")

    def test_invalid_profile_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown SoftHier power profile"):
            MODELS.validate_power_profile("unphysical")


if __name__ == "__main__":
    unittest.main()
