import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("llm_validate", Path(__file__).resolve().parents[1] / "validate_run.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        for folder in ("logs", "generated", "traces"):
            (self.run / folder).mkdir()
        self.write("logs/simulator.log", "[LLMForward] Finite hidden output: PASS\n[LLMForward] Application exit: 0\n")
        self.write("summary.txt", "simulator: 0\n3dice_server: 0\n")
        self.write("generated/system_config.json", json.dumps({
            "thermal_feedback": {"components": [{"path": "/chip/a"}]},
            "floorplan": {"elements": [{"name": "chip/a"}, {"name": "chip/gap"}]}}))
        item = {"path": "/chip/a", "dynamic_w": 1, "leakage_w": 0, "total_w": 1, "temperature_c": 27}
        self.records = [
            {"phase": "init", "window": {"start_ps": 0, "end_ps": 0}, "components": []},
            {"phase": "update", "window": {"start_ps": 0, "end_ps": 10}, "components": [item]},
            {"phase": "final", "window": {"start_ps": 10, "end_ps": 12}, "components": [item.copy()]},
        ]
        self.write_trace()
        self.write("traces/3dice_power_traces.txt", "1 0\n1 0\n-1 -1\n")
        self.write("traces/component_temperatures.csv", "phase,start_ps,end_ps,component_path,power_w,temperature_c\nupdate,0,10,/chip/a,1,27\nfinal,10,12,/chip/a,1,28\n")

    def write(self, name, text):
        (self.run / name).write_text(text)

    def write_trace(self):
        self.write("traces/power_hook_trace.jsonl", "".join(json.dumps(r)+"\n" for r in self.records))

    def test_valid_completion(self):
        result = validator.validate_run(self.run)
        self.assertEqual(result["component_count"], 1)
        self.assertEqual(result["max_temperature_c"], 28)

    def test_eoc_zero_process_is_not_application_success(self):
        self.write("logs/simulator.log", "[LLMForward] Application exit: 5\n")
        with self.assertRaises(ValueError):
            validator.validate_run(self.run, coupled=False)

    def test_missing_final_or_empty_trace(self):
        for records in ([], self.records[:-1]):
            self.records = records
            self.write_trace()
            with self.assertRaises(ValueError):
                validator.validate_run(self.run)

    def test_inexact_coverage(self):
        self.records[1]["components"] *= 2
        self.write_trace()
        with self.assertRaises(ValueError):
            validator.validate_run(self.run)

    def test_nonfinite_power(self):
        self.records[1]["components"][0]["total_w"] = float("nan")
        self.write_trace()
        with self.assertRaises(ValueError):
            validator.validate_run(self.run)

    def test_missing_sentinel(self):
        self.write("traces/3dice_power_traces.txt", "1 0\n")
        with self.assertRaises(ValueError):
            validator.validate_run(self.run)

    def test_server_failure(self):
        self.write("summary.txt", "simulator: 0\n3dice_server: 1\n")
        with self.assertRaises(ValueError):
            validator.validate_run(self.run)
