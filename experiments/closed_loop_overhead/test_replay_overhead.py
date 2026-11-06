"""Capture/replay accounting and acceptance tests; full B8 runs stay manual."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .analyze import summarize_events
from .replay_analysis import combine, load_results, summarize_capture, summarize_replay
from . import replay_overhead as benchmark
from . import replay_recovery as recovery

NS = 10**9
SLOT = 20_000_000


def event(phase, start, end, sim_ps=0, value=0, pid=7):
    return dict(actor="test", phase=phase, start_ns=start * NS, end_ns=end * NS,
                pid=pid, sim_ps=sim_ps, value=value)


def capture_events():
    events = [event("roi_start", 10, 10, 269000), event("roi_end", 90, 90, 269000 + 2 * SLOT),
              event("exchange", 20, 60, SLOT, SLOT), event("power_capture", 20, 25, SLOT)]
    counter = event("inline_power_accounting", 90, 90, 269000 + 2 * SLOT, 8 * NS)
    counter.update(kind="counter", method_version=1, scope="roi", calls=100,
                   window_start_ns=10 * NS, window_end_ns=90 * NS,
                   window_start_ps=269000, window_end_ps=269000 + 2 * SLOT,
                   dram_callbacks_registered=True)
    return [*events, counter]


def replay_fixture():
    events = [event("replay_start", 130, 130), event("replay_end", 160, 160, 2 * SLOT),
              event("replay_power", 130, 131), event("thermal_step", 132, 140),
              event("replay_power", 145, 146, SLOT), event("thermal_step", 147, 155, SLOT),
              event("thermal_solve", 148, 154, SLOT),  # nested, never added twice
              event("factorization", 122, 125), event("mesh_cells", 125, 125, value=67139)]
    settings = dict(interval_ps=SLOT, replay_simulated_ps=2 * SLOT, native_cells=67139, thermal_threads=1)
    summary = dict(status="time_limit", profile="temperature_aware", single_pass=False,
                   requested_cycles=None, slot_ps=SLOT, step_ps=SLOT, thermal_cells=67139,
                   factorization_threads=1, slots=2, simulated_seconds=40e-6, stop_rounding_ps=0)
    return events, settings, summary


class AttributionTests(unittest.TestCase):
    def capture(self):
        return summarize_capture(capture_events(), 5 * NS, 100 * NS, SLOT)

    def replay(self):
        events, settings, summary = replay_fixture()
        return summarize_replay(events, 120 * NS, 170 * NS, summary, settings)

    def test_power_enabled_capture_has_no_thermal_and_baseline_validation_stays_strict(self):
        capture = self.capture()
        self.assertEqual(capture["phases_seconds"], dict(architecture=32., power_estimation=13.,
                                                      thermal_advancement=0., coupling_output=35.))
        self.assertEqual(capture["power_accounting"]["seconds"], 8.)
        with self.assertRaisesRegex(ValueError, "architecture-only"):
            summarize_events(capture_events(), 5 * NS, 100 * NS, False, SLOT, True)
        with self.assertRaisesRegex(ValueError, "thermal solver"):
            summarize_capture(capture_events() + [event("thermal_step", 30, 40)], 5 * NS, 100 * NS, SLOT)

    def test_replay_partition_excludes_nested_solver_and_has_zero_architecture(self):
        replay = self.replay()
        self.assertEqual(replay["phases_seconds"], dict(architecture=0., power_estimation=2.,
                                                     thermal_advancement=16., coupling_output=12.))
        self.assertAlmostEqual(sum(replay["phases_seconds"].values()), replay["loop_seconds"])

    def test_capture_charged_once_and_full_workflow_reconciles(self):
        result = combine(self.capture(), self.replay(), 0)
        self.assertEqual(result["capture_count"], 1)
        self.assertEqual(result["runtime_seconds"], 110.)
        self.assertEqual(result["end_to_end_seconds"], 170.)
        self.assertEqual(result["phases_seconds"]["architecture"], 32.)
        self.assertEqual(sum(result["excluded_seconds"].values()), 60.)
        self.assertEqual(sum(result["power_components_seconds"].values()), result["phases_seconds"]["power_estimation"])
        # A longer replay does not multiply the one-time capture contribution.
        replay = self.replay()
        replay["simulated_ps"] *= 100
        longer = combine(self.capture(), replay, 0)
        self.assertEqual(longer["phases_seconds"], result["phases_seconds"])
        self.assertAlmostEqual(longer["host_seconds_per_simulated_second_with_capture"],
                               result["host_seconds_per_simulated_second_with_capture"] / 100)
        pilot = combine(self.capture(), self.replay(), 0, recovered_pilot=True)
        self.assertEqual(pilot["phases_seconds"], result["phases_seconds"])
        self.assertEqual(pilot["end_to_end_seconds"], result["end_to_end_seconds"])
        self.assertEqual(pilot["excluded_seconds"]["pilot_recovery_gap"], 20.)
        self.assertNotIn("recording_preparation", pilot["excluded_seconds"])

    def test_missing_inline_counter_and_wrong_counter_boundaries_fail(self):
        with self.assertRaisesRegex(ValueError, "direct inline-power"):
            summarize_capture(capture_events()[:-1], 5 * NS, 100 * NS, SLOT)
        events = capture_events()
        events[-1]["window_end_ps"] += 1
        with self.assertRaisesRegex(ValueError, "counter differs"):
            summarize_capture(events, 5 * NS, 100 * NS, SLOT)

    def test_rejects_incomplete_rounded_wrong_mesh_and_wrong_profile_replays(self):
        for key, value in (("status", "model_limit"), ("status", "interrupted"), ("slots", 1),
                           ("stop_rounding_ps", 1), ("thermal_cells", 67138), ("profile", "recorded"),
                           ("step_ps", SLOT // 2), ("factorization_threads", 2)):
            with self.subTest(key=key, value=value):
                events, settings, summary = replay_fixture()
                summary[key] = value
                with self.assertRaises(ValueError):
                    summarize_replay(events, 120 * NS, 170 * NS, summary, settings)

    def test_missing_duplicate_or_overlapping_steps_fail(self):
        for change in ("missing", "duplicate", "overlap", "between_slots", "factor"):
            events, settings, summary = replay_fixture()
            if change == "missing":
                events.pop(3)
            elif change == "duplicate":
                events[5]["sim_ps"] = 0
            elif change == "overlap":
                events[2]["end_ns"] = 133 * NS
            elif change == "between_slots":
                events[4]["start_ns"] = 139 * NS
            else:
                events.append(event("factorization", 125, 126))
            with self.subTest(change=change), self.assertRaises(ValueError):
                summarize_replay(events, 120 * NS, 170 * NS, summary, settings)


class ResultTests(unittest.TestCase):
    def test_recovered_pilot_runs_only_replay_and_measured_capture_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "pilot"
            for name in ("capture/timing", "capture/logs", "capture/traces", "capture/results/simulator", "recording"):
                (folder / name).mkdir(parents=True)
            for name in ("capture/logs/simulator.log", "capture/traces/power_hook_trace.jsonl",
                         "capture/results/simulator/gvsoc_config.json", "recording/recording.json"):
                (folder / name).write_text("{}\n")
            capture = AttributionTests().capture()
            events, settings, summary = replay_fixture()
            settings.update(cpus=[0], max_host_seconds=None)
            summary["recording_sha256"] = {}
            item = dict(id="pilot", block=0, pilot=True)
            saved = dict(item, capture=capture, workflow_start_ns=0, reuse_capture={"archive": "original"})
            def execute(*args):
                (folder / "replay").mkdir()
                (folder / "replay/summary.json").write_text(json.dumps(summary))
                return 120 * NS, 170 * NS
            with patch.object(benchmark, "capture_setup") as setup, patch.object(benchmark, "record") as record, \
                    patch.object(benchmark, "validate_capture", return_value=(capture, {"sha256": {}, "power_interval_ps": SLOT})), \
                    patch.object(benchmark.runner, "environment", return_value={}), \
                    patch.object(benchmark.runner, "execute", side_effect=execute) as executed, \
                    patch.object(benchmark, "read_events", return_value=events), \
                    patch.object(benchmark, "verify_identities"):
                result = benchmark.workflow(folder, item, dict(workload={}, manifest={"architectural_signature": {}}),
                                            settings, reuse_capture=saved)
                self.assertEqual(result["status"], "complete")
                self.assertEqual(result["capture"], capture)
                self.assertEqual(result["combined"]["capture_count"], 1)
                self.assertIn("pilot_recovery_gap", result["combined"]["excluded_seconds"])
                setup.assert_not_called()
                record.assert_not_called()
                executed.assert_called_once()
                with self.assertRaisesRegex(ValueError, "only an explicitly recovered pilot"):
                    benchmark.workflow(folder, dict(item, pilot=False), {}, settings, reuse_capture=saved)

    def test_pilot_recovery_preserves_source_and_reference_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            relative = "experiments/closed_loop_overhead/replay_overhead.py"
            snapshot = root / "source_snapshot" / relative
            snapshot.parent.mkdir(parents=True)
            snapshot.write_bytes((benchmark.ROOT / relative).read_bytes())
            hashes = {relative: benchmark.digest(snapshot)}
            signature = {"cycles": 123}
            ref = dict(directory=root / "reference", source=root / "source", input_sha256={"native": "same"},
                       source_sha256=hashes, manifest={"architectural_signature": signature})
            identity = dict(directory=str(ref["directory"]), run=str(ref["source"]), input_sha256=ref["input_sha256"])
            schedule = [dict(id="pilot", block=0, pilot=True), dict(id="block01", block=1, pilot=False)]
            manifest = dict(experiment="capture_replay_overhead_v1", status="failed", schedule=schedule,
                            reference=identity, architectural_signature=signature, source_sha256=hashes)
            row = dict(schedule[0], status="failed")
            self.assertEqual(recovery.check_scope(root, manifest, ref, row), [])
            bad = copy.deepcopy(ref)
            bad["input_sha256"]["native"] = "different"
            with self.assertRaisesRegex(ValueError, "unchanged reference"):
                recovery.check_scope(root, manifest, bad, row)
            bad = copy.deepcopy(ref)
            bad["source_sha256"]["Interface_scripts/overhead_timing.py"] = "different"
            with self.assertRaisesRegex(ValueError, "sources changed"):
                recovery.check_scope(root, manifest, bad, row)
            (root / "runs/block01").mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "precede all measured"):
                recovery.check_scope(root, manifest, ref, row)

    def test_pilot_and_failed_runs_excluded_no_invented_intervals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = AttributionTests().capture()
            replay = AttributionTests().replay()
            combined = combine(capture, replay, 0)
            schedule = [dict(id="pilot", block=0, pilot=True), dict(id="block01", block=1, pilot=False),
                        dict(id="block02", block=2, pilot=False)]
            manifest = dict(experiment="capture_replay_overhead_v1", status="failed", schedule=schedule,
                            settings=dict(replicates=2, seed=19), reference={}, architectural_signature={})
            (root / "study.json").write_text(json.dumps(manifest))
            for item in schedule:
                folder = root / "runs" / item["id"]
                folder.mkdir(parents=True)
                row = dict(item, status="failed" if item["id"] == "block02" else "complete",
                           capture=capture, replay=replay, combined=combined, artifact_sha256={})
                (folder / "measurement.json").write_text(json.dumps(row))
            with patch("experiments.closed_loop_overhead.replay_analysis.validate_measurement") as checked:
                result = load_results(root)
                self.assertEqual(checked.call_count, 1)
                self.assertEqual(result["included_runs"], ["block01"])
                self.assertIsNone(result["runtime_ci95_seconds"])
                self.assertEqual(result["combined"]["runtime_seconds"], 110.)
                with self.assertRaisesRegex(ValueError, "complete capture/replay study"):
                    load_results(root, require_complete=True)

    def test_plan_defaults_and_command_keep_full_workflow_manual(self):
        args = benchmark.parse_args(["plan"])
        self.assertEqual((args.max_seconds, args.replicates, args.resume), (1., 1, False))
        command = benchmark.replay_command(Path("recording"), Path("output"),
            dict(interval_ps=SLOT, replay_simulated_ps=10**12, thermal_threads=1))
        self.assertIn("temperature_aware", command)
        self.assertNotIn("--single-pass", command)
        self.assertNotIn("--dense-maps", command)

    def test_resume_skips_completed_and_preserves_failed_workflows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ref = dict(directory=root / "reference", source=root / "reference/runs/block01_closed_loop_50",
                       manifest=dict(settings=dict(case="decode_b8_kv1024", layer=1, seed=19,
                            cpus=[min(benchmark.os.sched_getaffinity(0))], interval_ps=SLOT),
                            architectural_signature={}), input_sha256={}, source_sha256={})
            calls = []
            def fake_workflow(folder, item, *_):
                calls.append(item["id"])
                folder.mkdir(parents=True)
                (folder / "measurement.json").write_text(json.dumps(dict(item, status="complete")))
            with patch.object(benchmark, "HERE", root), patch.object(benchmark, "load_reference", return_value=ref), \
                    patch.object(benchmark.runner, "host_info", return_value={}), \
                    patch.object(benchmark, "workflow", side_effect=fake_workflow), \
                    patch.object(benchmark, "analyze"), \
                    patch("experiments.closed_loop_overhead.replay_analysis.validate_measurement"):
                benchmark.run(benchmark.parse_args(["run", "--prefix", "test"]))
                self.assertEqual(calls, ["pilot", "block01"])
                calls.clear()
                benchmark.run(benchmark.parse_args(["run", "--prefix", "test", "--resume"]))
                self.assertEqual(calls, [])
                folder = root / "results/test/runs/block01"
                p = folder / "measurement.json"
                d = json.loads(p.read_text()); d["status"] = "failed"; p.write_text(json.dumps(d))
                benchmark.run(benchmark.parse_args(["run", "--prefix", "test", "--resume"]))
                self.assertEqual(calls, ["block01"])
                self.assertEqual(len(list(folder.parent.glob("block01_failed_*"))), 1)
                with self.assertRaisesRegex(ValueError, "resume requires"):
                    benchmark.run(benchmark.parse_args(["run", "--prefix", "test", "--resume", "--max-seconds", ".5"]))


if __name__ == "__main__":
    unittest.main()
