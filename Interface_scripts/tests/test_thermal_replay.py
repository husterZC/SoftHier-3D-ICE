"""Thermal replay physics, transport, boundaries, and interruption regressions."""

import gzip
import csv
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
try:
    import numpy as np
except ImportError:
    raise unittest.SkipTest("thermal replay requires numpy (use the simulator Python environment)")

from experiments.thermal_replay.activity import Activity, LeakageModel, digest, load_recording, read_trace, roi_from_log, write_json
from experiments.thermal_replay.ice import IceClient, prepare_stack
from experiments.thermal_replay.record import capture, read_environment, record, recording_stack
from experiments.thermal_replay.replay import run
from experiments.thermal_replay.validate import compare_replay


SERVER = ROOT / "3D-ICE/bin/3D-ICE-Server"
STACK = """material SILICON :
 thermal conductivity 1.30e-4 ;
 volumetric heat capacity 1.628e-12 ;
top heat sink :
 heat transfer coefficient 1e-07 ;
 temperature 300 ;
dimensions :
 chip length 1000, width 1000 ;
 cell length 100, width 100 ;
 non-uniform true;
layer PCB :
 height 50 ;
 material SILICON ;
die IC :
 source 50 SILICON ;
stack:
 die TOP_DIE IC floorplan "floorplan_nopower.flp" ;
 layer PACKAGE PCB ;
solver:
 transient step 1e-05, slot 0.0001 ;
 initial temperature 300 ;
 numofcores 1 ;
output:
 Tflp(TOP_DIE, "average.txt", average, slot);
 Tmap(TOP_DIE, "map.txt", slot);
"""
FLOORPLAN = """chip__logic :
 position 0, 0;
 dimension 500, 1000;
 discretization 5, 10;
chip__memory :
 position 500, 0;
 dimension 500, 1000;
 discretization 5, 10;
"""


def contract():
    names = ("logic", "memory")
    return {
        "contract": {"name": "3dice-cosim-system", "version": 1},
        "producer": {"name": "test"},
        "metadata": {"component_power_model_version": 2, "technology_node": "5nm",
                     "power_model_profile": "constant", "power_voltage_v": .7,
                     "power_frequency_hz": 1e9,
                     "components": [{"simulator_path": "/chip/" + n, "kind": k}
                                    for n, k in zip(names, ("core", "memory"))],
                     "power_model_specs": {k: {"technology_nodes": {"5nm": {
                         "leakage_doubling_temperature_c": v}}}
                         for k, v in (("memory", 27.), ("light_redmule", 22.))}},
        "geometry": {"chip": {"type": "die", "shape": [1000., 1000.], "offset": [0., 0.],
                              "subs": {n: {"type": "comp", "shape": [500., 1000.],
                                           "offset": [i * 500., 0.], "subs": {}} for i, n in enumerate(names)}}},
        "floorplan": {"elements": [{"name": "chip/" + n, "power": {"column": n}} for n in names]},
        "power_trace": {"format": "whitespace-float-rows", "unit": "W", "columns": list(names)},
        "thermal_feedback": {"temperature_unit": "C", "initial_temperature_c": 26.85,
                             "components": [{"path": "/chip/" + n, "power_column": n,
                                             "floorplan_elements": ["chip/" + n],
                                             "aggregation": "area-weighted-average"} for n in names]},
    }


def trace_entry(phase, start, end, dynamic=(.2, .3), leakage=(.01, .02)):
    return {"protocol": {"name": "gvsoc-power-hook", "version": 1}, "phase": phase,
            "temperature_unit": "celsius", "window": {"start_ps": start, "end_ps": end},
            "components": [] if phase == "init" else [
                {"path": "/chip/" + n, "scope": "subtree", "dynamic_w": d, "leakage_w": l,
                 "total_w": d + l, "temperature_c": 26.85}
                for n, d, l in zip(("logic", "memory"), dynamic, leakage)]}


class ActivityTests(unittest.TestCase):
    def test_fractional_boundaries_preserve_energy_and_do_not_repeat_startup_or_tail(self):
        activity = Activity(np.array([0, 10, 20, 23]), np.array([[100.], [2.], [900.]]),
                            np.ones((3, 1)), 8, 17)
        expected_cycle = (2 * 100 + 7 * 2) * 1e-12
        np.testing.assert_allclose(activity.cycle_energy, [expected_cycle, 9e-12])
        end = 8 + 103 * 9
        energy = sum(activity.average(t, min(t + 10, end))[0][0] * (min(10, end - t) * 1e-12)
                     for t in range(0, end, 10))
        self.assertAlmostEqual(energy, 800e-12 + 103 * expected_cycle, places=18)
        self.assertAlmostEqual(activity.average(8 + 100_000 * 9, 17 + 100_000 * 9)[0][0],
                               expected_cycle / 9e-12, places=8)

    def test_leakage_is_interpolated_at_grid_points_for_logic_and_actual_sram_reference(self):
        model = LeakageModel(contract(), ["/chip/logic", "/chip/memory"])
        np.testing.assert_allclose(model.factors(np.array([32.5, 47.5])),
                                   [(1 + 2 ** (15 / 22)) / 2, (2 ** (15 / 27) + 2 ** (30 / 27)) / 2])
        np.testing.assert_allclose(model.factors(np.array([0., 200.])), [1., 2 ** (100 / 27)])

    def test_exact_timestamps_required_and_incomplete_trace_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace"
            path.write_text(json.dumps(trace_entry("init", 0, 0)) + "\n" +
                            json.dumps(trace_entry("update", 0, 10)) + "\n")
            with self.assertRaisesRegex(ValueError, "incomplete"):
                read_trace(path, ["/chip/logic", "/chip/memory"])
            path.write_text("Execution period is 100 ns\n")
            with self.assertRaisesRegex(ValueError, "timer"):
                roi_from_log(path)
            path.write_text("THERMAL_REPLAY_ROI_START_PS=17\nTHERMAL_REPLAY_ROI_END_PS=37\n")
            self.assertEqual(roi_from_log(path), (17, 37))

    def test_trace_rejects_gaps_and_duplicate_components(self):
        for corrupt in ("gap", "duplicate"):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as directory:
                entry = trace_entry("final", 1 if corrupt == "gap" else 0, 10)
                if corrupt == "duplicate":
                    entry["components"].append(entry["components"][0])
                path = Path(directory) / "trace"
                path.write_text(json.dumps(trace_entry("init", 0, 0)) + "\n" + json.dumps(entry) + "\n")
                with self.assertRaises(ValueError):
                    read_trace(path, ["/chip/logic", "/chip/memory"])

    def test_record_hook_sets_voltage_once_without_thermal_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            updates = [{"path": "/chip/logic", "temperature_c": 26.85, "voltage_v": .7}]
            write_json(directory / "config.json", {"temperatures": updates})
            for phase in ("init", "update", "final"):
                write_json(directory / "request.json", trace_entry(phase, 0, 0 if phase == "init" else 10))
                subprocess.run([sys.executable, str(ROOT / "experiments/thermal_replay/record_hook.py"),
                                "--phase", phase, "--config", str(directory / "config.json"),
                                "--request", str(directory / "request.json"),
                                "--response", str(directory / "response.json")], check=True, timeout=10)
                response = json.loads((directory / "response.json").read_text())
                self.assertEqual(response["temperatures"], updates if phase == "init" else [])

    def test_saved_environment_is_data_and_preserves_quoted_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.env"
            path.write_text("A='/a path/file'\nB=''\nC='$(touch sentinel)'\n")
            self.assertEqual(read_environment(path), {"A": "/a path/file", "B": "", "C": "$(touch sentinel)"})


class RecordingStackTests(unittest.TestCase):
    def test_cooling_override_uses_si_units_and_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ice.stk"
            path.write_text(STACK)
            self.assertEqual(recording_stack(path, None), (STACK, None))
            modified, variant = recording_stack(path, 15000)
            coefficient = re.search(r"coefficient\s+(\S+)\s*;", modified)[1]
            self.assertEqual(float(coefficient), 1.5e-8)
            self.assertEqual(modified.replace(coefficient, "1e-07"), STACK)
            self.assertEqual(variant["old_w_per_m2_k"], 100000)
            self.assertEqual(variant["new_w_per_m2_k"], 15000)
            self.assertEqual(variant["new_w_per_um2_k"], 1.5e-8)
            self.assertEqual(variant["source_stack_sha256"], digest(path))
            self.assertEqual(path.read_text(), STACK)
            for invalid in (0, -1, float("nan"), float("inf"), 1e-320):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "coefficient"):
                    recording_stack(path, invalid)
            for invalid in (STACK.replace("top heat sink", "bottom heat sink"), STACK + STACK):
                path.write_text(invalid)
                with self.assertRaisesRegex(ValueError, "missing or ambiguous"):
                    recording_stack(path, 15000)


class ReplayCliTests(unittest.TestCase):
    def test_retired_options_are_rejected_and_default_cap_is_one_second(self):
        command = [sys.executable, "-m", "experiments.thermal_replay", "replay"]
        help_result = subprocess.run([*command, "--help"], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertIn("default: 1 s", help_result.stdout)
        for option, value in (("--until-converged", []), ("--tolerance", [".01"]),
                              ("--consecutive", ["5"]), ("--minimum", ["10"])):
            with self.subTest(option=option):
                self.assertNotIn(option, help_result.stdout)
                result = subprocess.run([*command, "--recording", "unused", "--output", "unused",
                                         option, *value], cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn("unrecognized arguments", result.stderr)


class AnalysisBoundaryTests(unittest.TestCase):
    def test_legacy_and_new_boundaries_produce_the_same_data(self):
        from experiments.thermal_replay.analyze import read_cycles, csv_file
        summary = {"layers_per_cycle": 10, "layer_start_ps": 25, "period_ps": 250,
                   "cycles_completed": 2, "layers_completed": 20}
        expected = [{"cycle": 1, "time_ps": 275, "layers_completed": 10},
                    {"cycle": 2, "time_ps": 525, "layers_completed": 20}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cycles.csv"
            path.write_text("cycle,time_ps,waveform_delta_c,full_state_delta_c,stable_cycles,converged\n"
                            "1,275,,,0,False\n2,525,.01,.001,1,True\n")
            self.assertEqual(read_cycles(path, summary), expected)
            csv_file(path, expected)
            self.assertEqual(read_cycles(path, summary), expected)
            path.write_text("cycle,time_ps,layers_completed\n1,275,10\n2,525,19\n")
            with self.assertRaisesRegex(ValueError, "Layer count"):
                read_cycles(path, summary)
            path.write_text("cycle,time_ps,layers_completed\n1,275,10\n2,526,20\n")
            with self.assertRaisesRegex(ValueError, "Cycle boundary"):
                read_cycles(path, summary)
            csv_file(path, [], fieldnames=["cycle", "time_ps", "layers_completed"])
            self.assertEqual(read_cycles(path, dict(summary, cycles_completed=0, layers_completed=0)), [])


class ReplayStepTests(unittest.TestCase):
    def test_step_defaults_to_slot_and_override_preserves_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            recording = directory / "recording"
            recording.mkdir()
            archived = STACK.replace("step 1e-05, slot 0.0001", "step 2e-06, slot 2e-05")
            (recording / "ice.stk").write_text(archived)
            (recording / "floorplan.flp").write_text(FLOORPLAN)
            for name, requested, expected in (("default", None, 20_000_000),
                                               ("fine", 2e-6, 2_000_000)):
                with self.subTest(name=name):
                    output = directory / name
                    output.mkdir()
                    self.assertEqual(prepare_stack(recording, output, thermal_step=requested),
                                     (expected, 20_000_000))
                    self.assertIn(f"transient step {expected / 1e12:.12g}, slot 2e-05",
                                  (output / "ice.stk").read_text())
                    self.assertEqual((recording / "ice.stk").read_text(), archived)
            for invalid in (0, -1, float("nan"), float("inf"), 1e-13, 3e-6, 4e-5):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "thermal"):
                    prepare_stack(recording, directory, thermal_step=invalid)


@unittest.skipUnless(SERVER.is_file(), "requires built 3D-ICE server")
class ReplayIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="thermal-replay-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        source = self.directory / "source"
        for name in ("generated/3dice", "logs", "traces"):
            (source / name).mkdir(parents=True)
        write_json(source / "generated/system_config.json", contract())
        (source / "generated/3dice/ice.stk").write_text(STACK)
        (source / "generated/3dice/floorplan_nopower.flp").write_text(FLOORPLAN)
        (source / "logs/simulator.log").write_text("THERMAL_REPLAY_ROI_START_PS=25000000\n"
                                                  "THERMAL_REPLAY_ROI_END_PS=275000000\n")
        entries = [trace_entry("init", 0, 0), trace_entry("update", 0, 100_000_000),
                   trace_entry("update", 100_000_000, 200_000_000, (.3, .1)),
                   trace_entry("final", 200_000_000, 285_000_000, (.1, .2))]
        (source / "traces/power_hook_trace.jsonl").write_text("\n".join(map(json.dumps, entries)) + "\n")
        self.source = source
        self.recording = self.directory / "recording"
        record(source, self.recording)

    def test_record_prefers_geometry_only_source_floorplan(self):
        powered = FLOORPLAN.replace("discretization 5, 10;", "discretization 5, 10;\n power values 999;")
        (self.source / "generated/3dice/floorplan.flp").write_text(powered)
        output = self.directory / "second_recording"
        record(self.source, output)
        self.assertEqual((output / "floorplan.flp").read_text(), FLOORPLAN)

    def test_startup_retries_a_self_connected_socket(self):
        echoed = socket.socket()
        self.addCleanup(echoed.close)
        echoed.settimeout(1)
        echoed.bind(("127.0.0.1", 0))
        echoed.connect(echoed.getsockname())
        original_connect = socket.create_connection
        attempts = 0

        def connect(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            return echoed if attempts == 1 else original_connect(*args, **kwargs)

        with patch("experiments.thermal_replay.ice.socket.create_connection", side_effect=connect):
            result = run(self.recording, self.directory / "startup_retry", cycles=1)
        self.assertEqual(result["status"], "complete")
        self.assertGreater(attempts, 1)
        self.assertEqual(echoed.fileno(), -1)

    def test_persistent_socket_matches_native_trace_mode_and_preserves_package(self):
        output = self.directory / "single"
        result = run(self.recording, output, profile="recorded", single_pass=True, dense_maps=True,
                     thermal_step=1e-5)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["step_ps"], 10_000_000)
        local = self.directory / "local"
        local.mkdir()
        (local / "ice.stk").write_text(STACK)
        (local / "floorplan_nopower.flp").write_text(FLOORPLAN)
        (local / "power.txt").write_text(".21 .32\n.31 .12\n.11 .22\n-1 -1\n")
        subprocess.run([str(SERVER), "ice.stk", "--power-trace", "power.txt", "--until-minus-one"],
                       cwd=local, check=True, capture_output=True, timeout=15)
        expected = np.loadtxt(local / "average.txt", comments="%")
        actual = np.loadtxt(output / "floorplan_temperatures.txt", comments="%")
        np.testing.assert_array_equal(actual, expected)
        np.testing.assert_array_equal(np.loadtxt(output / "temperature_map.txt", comments="%"),
                                      np.loadtxt(local / "map.txt", comments="%"))
        state = np.load(output / "final_state.npz")["temperatures_c"]
        self.assertEqual(len(state), 200)  # 100 die + 100 passive package cells.
        self.assertGreater(state.min(), 26.85)
        expected_energy = (.21 + .32 + .31 + .12) * .0001 + (.11 + .22) * .000085
        self.assertAlmostEqual(result["total_energy_j"], expected_energy, places=14)
        reference = self.source / "results/3dice/output_top_die_flp_avg.txt"
        reference.parent.mkdir(parents=True)
        reference.write_bytes((local / "average.txt").read_bytes())
        self.assertTrue(compare_replay(output, self.source, single_pass=True)["passed"])
        runtime_stack = output / "ice.stk"
        original_stack = runtime_stack.read_text()
        runtime_stack.write_text(original_stack.replace("step 1e-05", "step 0.0001"))
        with self.assertRaisesRegex(ValueError, "integration steps"):
            compare_replay(output, self.source, single_pass=True)
        runtime_stack.write_text(original_stack)
        modified = dict(result, dynamic_energy_j=result["dynamic_energy_j"] * 1.1)
        write_json(output / "summary.json", modified)
        self.assertFalse(compare_replay(output, self.source, single_pass=True)["passed"])

    def test_repetition_keeps_fractional_period_and_cold_start(self):
        output = self.directory / "repeat"
        result = run(self.recording, output, cycles=10, profile="constant")
        self.assertEqual(result["cycles_completed"], 10)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["step_ps"], result["slot_ps"])
        self.assertEqual(result["slots"], 26)  # ceil((25 + 10*250) / 100), not 30 slots.
        with gzip.open(output / "component_temperatures_c.csv.gz", "rt") as stream:
            stream.readline()
            np.testing.assert_allclose([float(v) for v in stream.readline().split(",")], [0., 26.85, 26.85], atol=1e-12)
        cycles = np.genfromtxt(output / "cycles.csv", delimiter=",", names=True)
        self.assertEqual(cycles.dtype.names, ("cycle", "time_ps", "layers_completed"))
        np.testing.assert_array_equal(cycles["time_ps"], 25_000_000 + np.arange(1, 11) * 250_000_000)
        np.testing.assert_array_equal(cycles["layers_completed"], np.arange(1, 11))
        block = self.directory / "block"
        record(self.source, block, layers_per_cycle=10)
        block_result = run(block, self.directory / "block_output", cycles=10, profile="constant")
        self.assertEqual(block_result["layers_completed"], 100)
        self.assertEqual(block_result["slots"], result["slots"])
        self.assertEqual(block_result["total_energy_j"], result["total_energy_j"])

    def test_interruption_and_failure_close_server_and_retain_status(self):
        for exception, status in ((KeyboardInterrupt(), "interrupted"), (RuntimeError("test failure"), "failure")):
            with self.subTest(status=status):
                output = self.directory / status
                with patch.object(IceClient, "advance", side_effect=exception):
                    with self.assertRaises(type(exception)):
                        run(self.recording, output, cycles=2)
                summary = json.loads((output / "summary.json").read_text())
                self.assertEqual(summary["status"], status)
                self.assertTrue((output / "server.log").exists())

    def test_time_cap_and_model_limit_statuses(self):
        result = run(self.recording, self.directory / "limited", max_seconds=.00015)
        self.assertEqual(result["status"], "time_limit")
        original = IceClient.temperatures
        calls = 0
        def hot(ice, entries):
            nonlocal calls
            calls += 1
            return original(ice, entries) if calls == 1 else np.array([126., 30.])
        with patch.object(IceClient, "temperatures", hot):
            result = run(self.recording, self.directory / "hot", cycles=2)
        self.assertEqual(result["status"], "model_limit")

    def test_stable_temperatures_run_to_requested_limits_with_two_state_reads(self):
        trace = self.source / "traces/power_hook_trace.jsonl"
        entries = [json.loads(line) for line in trace.read_text().splitlines()]
        for entry in entries:
            for component in entry["components"]:
                component.update(dynamic_w=0., leakage_w=0., total_w=0.)
        trace.write_text("\n".join(map(json.dumps, entries)) + "\n")
        recording = self.directory / "zero_power"
        record(self.source, recording, layers_per_cycle=10)
        for count, slots, cycles, status in ((None, 200, 79, "time_limit"),
                                            (40, 101, 40, "complete"), (100, 200, 79, "time_limit")):
            with self.subTest(count=count):
                output = self.directory / f"stable_{count}"
                with patch.object(IceClient, "state", autospec=True, side_effect=IceClient.state) as state:
                    result = run(recording, output, max_seconds=.02, cycles=count)
                self.assertEqual(state.call_count, 2)
                self.assertEqual((result["slots"], result["cycles_completed"], result["status"]),
                                 (slots, cycles, status))
                self.assertEqual(result["layers_completed"], cycles * 10)
                self.assertAlmostEqual(result["final_peak_component_c"], 26.85, places=3)
                self.assertTrue({"convergence", "last_cycle", "boundary_state_method"}.isdisjoint(result))
                with (output / "cycles.csv").open() as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(int(rows[-1]["layers_completed"]), cycles * 10)

    def test_explicit_bounds_import_logs_without_native_timer_markers(self):
        (self.source / "logs/simulator.log").write_text("Execution period is 250000 ns\n")
        recording = self.directory / "explicit_bounds"
        manifest = record(self.source, recording, start=25_000_000, end=275_000_000,
                          layers_per_cycle=10)
        self.assertEqual(manifest["period_ps"], 250_000_000)
        result = run(recording, self.directory / "explicit_bounds_replay", cycles=1)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["layers_completed"], 10)

    def test_recording_cooling_override_is_checksummed_and_capture_validates_early(self):
        output = self.directory / "h15000"
        original = (self.source / "generated/3dice/ice.stk").read_bytes()
        manifest = record(self.source, output, heat_transfer_coefficient=15000)
        loaded, _, _ = load_recording(output)
        self.assertEqual(loaded, manifest)
        self.assertEqual(manifest["sha256"]["ice.stk"], digest(output / "ice.stk"))
        self.assertNotEqual(manifest["sha256"]["ice.stk"], digest(self.source / "generated/3dice/ice.stk"))
        self.assertEqual((self.source / "generated/3dice/ice.stk").read_bytes(), original)
        with patch("experiments.thermal_replay.record.subprocess.run") as provider:
            with self.assertRaisesRegex(ValueError, "coefficient"):
                capture(self.source, self.directory / "bad_capture", ROOT / "unused-provider",
                        heat_transfer_coefficient=-1)
            provider.assert_not_called()
        self.assertFalse((self.directory / "bad_capture").exists())

    def test_checksum_and_whole_program_repetition_rejected(self):
        whole = self.directory / "whole"
        record(self.source, whole, whole_trace=True)
        with self.assertRaisesRegex(ValueError, "whole-program"):
            run(whole, self.directory / "bad", cycles=2)
        (self.recording / "floorplan.flp").write_text("modified")
        with self.assertRaisesRegex(ValueError, "checksum"):
            run(self.recording, self.directory / "bad2", cycles=2)


if __name__ == "__main__":
    unittest.main()
