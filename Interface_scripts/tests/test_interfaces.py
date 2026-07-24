#!/usr/bin/env python3

import argparse
import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


INTERFACE_DIR = Path(__file__).resolve().parents[1]
CO_SIMULATION_DIR = INTERFACE_DIR / "co-simulation"
sys.path.insert(0, str(INTERFACE_DIR))
sys.path.insert(0, str(CO_SIMULATION_DIR))

from ice_trace_adapter import (  # noqa: E402
    adapt_slot,
    build_power_sources,
    follow_raw_trace,
    validate_floorplan_entries,
)
from system_contract import ContractError, validate_contract  # noqa: E402


HOOK_PATH = CO_SIMULATION_DIR / "3dice_power_hook.py"
HOOK_SPEC = importlib.util.spec_from_file_location(
    "gvsoc_3dice_power_hook", str(HOOK_PATH)
)
HOOK = importlib.util.module_from_spec(HOOK_SPEC)
HOOK_SPEC.loader.exec_module(HOOK)


def fixture_contract():
    components = {
        "compute": {
            "type": "comp",
            "shape": [8.0, 8.0],
            "offset": [0.0, 0.0],
            "subs": {},
        },
        "control": {
            "type": "comp",
            "shape": [8.0, 2.0],
            "offset": [0.0, 8.0],
            "subs": {},
        },
        "memory": {
            "type": "comp",
            "shape": [2.0, 10.0],
            "offset": [8.0, 0.0],
            "subs": {},
        },
    }
    return {
        "contract": {"name": "3dice-cosim-system", "version": 1},
        "producer": {"name": "test-provider"},
        "geometry": {
            "chip": {
                "type": "die",
                "shape": [10.0, 10.0],
                "offset": [0.0, 0.0],
                "subs": components,
            }
        },
        "floorplan": {
            "elements": [
                {
                    "name": "chip/compute",
                    "power": {"column": "compute_w"},
                },
                {
                    "name": "chip/control",
                    "power": {"constant_w": 0.125},
                },
                {
                    "name": "chip/memory",
                    "power": {"column": "memory_w"},
                },
            ]
        },
        "power_trace": {
            "format": "whitespace-float-rows",
            "unit": "W",
            "columns": ["compute_w", "memory_w"],
        },
        "thermal_feedback": {
            "temperature_unit": "C",
            "initial_temperature_c": 26.85,
            "components": [
                {
                    "path": "/chip/compute",
                    "power_column": "compute_w",
                    "floorplan_elements": ["chip/compute"],
                    "aggregation": "area-weighted-average",
                },
                {
                    "path": "/chip/memory",
                    "power_column": "memory_w",
                    "floorplan_elements": ["chip/memory"],
                    "aggregation": "area-weighted-average",
                },
            ],
        },
    }


class SystemContractTests(unittest.TestCase):
    def test_valid_contract(self):
        document = validate_contract(fixture_contract())
        self.assertEqual(document["producer"]["name"], "test-provider")

    def test_rejects_unknown_power_column(self):
        contract = fixture_contract()
        contract["floorplan"]["elements"][0]["power"]["column"] = "missing"
        with self.assertRaisesRegex(ContractError, "not declared"):
            validate_contract(contract)

    def test_rejects_floorplan_path_absent_from_geometry(self):
        contract = fixture_contract()
        contract["floorplan"]["elements"][0]["name"] = "chip/absent"
        with self.assertRaisesRegex(ContractError, "absent from geometry"):
            validate_contract(contract)

    def test_rejects_repeated_floorplan_power_column(self):
        contract = fixture_contract()
        contract["floorplan"]["elements"][1]["power"] = {
            "column": "compute_w"
        }
        with self.assertRaisesRegex(ContractError, "more than once"):
            validate_contract(contract)

    def test_rejects_unsafe_geometry_name(self):
        contract = fixture_contract()
        compute = contract["geometry"]["chip"]["subs"].pop("compute")
        contract["geometry"]["chip"]["subs"]["compute bad"] = compute
        contract["floorplan"]["elements"][0]["name"] = "chip/compute bad"
        with self.assertRaisesRegex(ContractError, "must contain only"):
            validate_contract(contract)

    def test_rejects_hierarchically_overlapping_component_paths(self):
        contract = fixture_contract()
        contract["thermal_feedback"]["components"][1]["path"] = (
            "/chip/compute/child"
        )
        with self.assertRaisesRegex(ContractError, "must not overlap"):
            validate_contract(contract)

    def test_rejects_unmapped_feedback_power_column(self):
        contract = fixture_contract()
        contract["thermal_feedback"]["components"].pop()
        with self.assertRaisesRegex(ContractError, "does not map power columns"):
            validate_contract(contract)


class TraceAdapterTests(unittest.TestCase):
    def setUp(self):
        self.document = validate_contract(fixture_contract())
        self.columns = self.document["power_trace"]["columns"]
        self.sources = build_power_sources(self.document)
        self.floorplan = [
            "chip__compute",
            "chip__control",
            "chip__memory",
        ]
        validate_floorplan_entries(self.floorplan, self.sources)

    def test_contract_mapping_and_override(self):
        raw_path = Path("/tmp/provider.trace")
        adapted = adapt_slot(
            [2.5, 0.75],
            self.columns,
            self.floorplan,
            self.sources,
            None,
            raw_path,
            1,
        )
        self.assertEqual(adapted, [2.5, 0.125, 0.75])

        overridden = adapt_slot(
            [2.5, 0.75],
            self.columns,
            self.floorplan,
            self.sources,
            0.5,
            raw_path,
            1,
        )
        self.assertEqual(overridden, [2.5, 0.5, 0.75])

    def test_rejects_wrong_raw_width(self):
        with self.assertRaisesRegex(RuntimeError, "expected 2 raw values"):
            adapt_slot(
                [1.0],
                self.columns,
                self.floorplan,
                self.sources,
                None,
                Path("/tmp/provider.trace"),
                7,
            )

    def test_follows_complete_rows_and_terminates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract_path = root / "system.json"
            floorplan_path = root / "floorplan.flp"
            raw_path = root / "raw.trace"
            output_path = root / "adapted.trace"
            done_path = root / "simulator.done"

            contract_path.write_text(
                json.dumps(self.document), encoding="utf-8"
            )
            floorplan_path.write_text(
                "\n".join(f"{name} :" for name in self.floorplan) + "\n",
                encoding="utf-8",
            )
            raw_path.write_text("2.5 0.75\n3.0 1.0\n", encoding="utf-8")
            done_path.touch()

            args = argparse.Namespace(
                system_config=str(contract_path),
                floorplan=str(floorplan_path),
                input=str(raw_path),
                output=str(output_path),
                done_file=str(done_path),
                default_power_w=None,
                poll=0.001,
                preserve_output=False,
            )
            self.assertEqual(follow_raw_trace(args), 0)
            self.assertEqual(
                output_path.read_text(encoding="utf-8").splitlines(),
                [
                    "2.5 0.125 0.75",
                    "3 0.125 1",
                    "-1 -1 -1",
                ],
            )

    def test_rejects_incomplete_final_row(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract_path = root / "system.json"
            floorplan_path = root / "floorplan.flp"
            raw_path = root / "raw.trace"
            output_path = root / "adapted.trace"
            done_path = root / "simulator.done"

            contract_path.write_text(
                json.dumps(self.document), encoding="utf-8"
            )
            floorplan_path.write_text(
                "\n".join(f"{name} :" for name in self.floorplan) + "\n",
                encoding="utf-8",
            )
            raw_path.write_text("2.5 0.75", encoding="utf-8")
            done_path.touch()

            args = argparse.Namespace(
                system_config=str(contract_path),
                floorplan=str(floorplan_path),
                input=str(raw_path),
                output=str(output_path),
                done_file=str(done_path),
                default_power_w=None,
                poll=0.001,
                preserve_output=False,
            )
            with self.assertRaisesRegex(RuntimeError, "incomplete final power row"):
                follow_raw_trace(args)


class PowerHookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.contract_path = self.root / "system.json"
        self.floorplan_path = self.root / "floorplan.flp"
        self.power_path = self.root / "power.trace"
        self.temperature_path = self.root / "temperatures.txt"
        self.history_path = self.root / "component_temperatures.csv"
        self.config_path = self.root / "hook.json"
        self.request_path = self.root / "request.json"
        self.response_path = self.root / "response.json"

        self.contract_path.write_text(
            json.dumps(validate_contract(fixture_contract())),
            encoding="utf-8",
        )
        self.floorplan_path.write_text(
            "\n".join(
                [
                    "chip__compute :",
                    "chip__control :",
                    "chip__memory :",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.temperature_path.write_text(
            "% Average temperatures for the floorplan of the die TOP_DIE\n"
            "% Time(s) chip__compute(K) chip__control(K) chip__memory(K)\n",
            encoding="utf-8",
        )
        self.config_path.write_text(
            json.dumps(
                {
                    "hook": {
                        "name": "3dice-gvsoc-power-hook",
                        "version": 1,
                    },
                    "system_config": str(self.contract_path),
                    "floorplan": str(self.floorplan_path),
                    "power_trace": str(self.power_path),
                    "temperature_output": str(self.temperature_path),
                    "temperature_history": str(self.history_path),
                    "poll_seconds": 0.001,
                    "timeout_seconds": 2.0,
                    "default_power_w": None,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, phase, start_ps, end_ps, components):
        self.request_path.write_text(
            json.dumps(
                {
                    "protocol": {
                        "name": "gvsoc-power-hook",
                        "version": 1,
                    },
                    "phase": phase,
                    "temperature_unit": "celsius",
                    "config_file": str(self.config_path),
                    "window": {
                        "start_ps": start_ps,
                        "end_ps": end_ps,
                    },
                    "components": components,
                }
            ),
            encoding="utf-8",
        )
        args = argparse.Namespace(
            phase=phase,
            request=str(self.request_path),
            response=str(self.response_path),
            config=str(self.config_path),
        )
        self.assertEqual(HOOK.run(args), 0)
        return json.loads(self.response_path.read_text(encoding="utf-8"))

    @staticmethod
    def samples(compute_temperature=26.85, memory_temperature=26.85):
        return [
            {
                "path": "/chip/compute",
                "scope": "subtree",
                "dynamic_w": 2.0,
                "leakage_w": 0.5,
                "total_w": 2.5,
                "temperature_c": compute_temperature,
            },
            {
                "path": "/chip/memory",
                "scope": "subtree",
                "dynamic_w": 0.5,
                "leakage_w": 0.25,
                "total_w": 0.75,
                "temperature_c": memory_temperature,
            },
        ]

    def publish_temperature_after_power_row(self, row_count, row):
        def worker():
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if self.power_path.exists():
                    complete = [
                        line
                        for line in self.power_path.read_text(
                            encoding="utf-8"
                        ).splitlines()
                        if line.strip()
                    ]
                    if len(complete) >= row_count:
                        with self.temperature_path.open(
                            "a", encoding="utf-8"
                        ) as stream:
                            stream.write(row + "\n")
                            stream.flush()
                        return
                time.sleep(0.001)
            raise RuntimeError("test thermal publisher timed out")

        thread = threading.Thread(target=worker)
        thread.start()
        return thread

    def test_complete_init_update_and_final_lifecycle(self):
        init_response = self.invoke("init", 0, 0, [])
        self.assertEqual(
            init_response["temperatures"],
            [
                {"path": "/chip/compute", "temperature_c": 26.85},
                {"path": "/chip/memory", "temperature_c": 26.85},
            ],
        )

        update_publisher = self.publish_temperature_after_power_row(
            1, "0.001 310 305 320"
        )
        update_response = self.invoke(
            "update", 0, 1000, self.samples()
        )
        update_publisher.join(timeout=2.0)
        self.assertFalse(update_publisher.is_alive())
        by_path = {
            item["path"]: item["temperature_c"]
            for item in update_response["temperatures"]
        }
        self.assertAlmostEqual(by_path["/chip/compute"], 36.85)
        self.assertAlmostEqual(by_path["/chip/memory"], 46.85)

        final_publisher = self.publish_temperature_after_power_row(
            2, "0.002 315 306 325"
        )
        final_response = self.invoke(
            "final", 1000, 1500, self.samples(36.85, 46.85)
        )
        final_publisher.join(timeout=2.0)
        self.assertFalse(final_publisher.is_alive())
        self.assertEqual(final_response["phase"], "final")

        power_rows = self.power_path.read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(power_rows[0], "2.5 0.125 0.75")
        self.assertEqual(power_rows[1], "2.5 0.125 0.75")
        self.assertEqual(power_rows[2], "-1 -1 -1")

        history = self.history_path.read_text(encoding="utf-8")
        self.assertIn("init,0,0,/chip/compute,,26.85", history)
        self.assertIn("update,0,1000,/chip/compute,2.5", history)
        self.assertIn("final,1000,1500,/chip/memory,0.75", history)


if __name__ == "__main__":
    unittest.main()
