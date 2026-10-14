import copy
import importlib.util
import json
from pathlib import Path
import unittest

NATIVE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("power_config", NATIVE / "power_config.py")
power_config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(power_config)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = json.loads((NATIVE.parent / "configs/dram3d_power.json").read_text())

    def test_default_is_explicit_estimate(self):
        power_config.validate_power_config(self.profile)
        self.assertIn("Uncalibrated", self.profile["provenance"])

    def test_missing_units_scope_fields_fail(self):
        for field in ("voltage_v", "background_current_a", "command_charge_c"):
            bad = copy.deepcopy(self.profile)
            del bad["rails"][0][field]
            with self.assertRaises(ValueError):
                power_config.validate_power_config(bad)
        for key, value in (("schema_version", 2), ("scope", "whole_stack"), ("memory_type", "DDR4"),
                           ("characterization_temperature_k", 0), ("provenance", ""), ("synthetic", True)):
            bad = copy.deepcopy(self.profile)
            bad[key] = value
            with self.assertRaises(ValueError):
                power_config.validate_power_config(bad)

    def test_nonfinite_negative_and_boolean_numbers_fail(self):
        for value in (float("nan"), float("inf"), -1, True, None, "1.2"):
            bad = copy.deepcopy(self.profile)
            bad["rails"][0]["command_charge_c"]["ACT"] = value
            with self.assertRaises(ValueError):
                power_config.validate_power_config(bad)

    def test_unknown_command_and_duplicate_rail_fail(self):
        bad = copy.deepcopy(self.profile)
        bad["rails"][0]["command_charge_c"]["TYPO"] = 0
        with self.assertRaises(ValueError):
            power_config.validate_power_config(bad)
        bad = copy.deepcopy(self.profile)
        bad["rails"].append(copy.deepcopy(bad["rails"][0]))
        with self.assertRaises(ValueError):
            power_config.validate_power_config(bad)


if __name__ == "__main__":
    unittest.main()
