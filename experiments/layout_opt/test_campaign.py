"""Checks for alias selection, isolated orchestration and replay provenance."""

import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from experiments.layout_opt import run as capture, run_parallel as parallel
from experiments.layout_opt.replay_study import check_complete
from experiments.layout_opt.transforms import ROOT, write_json
from experiments.llm_dram3d.opt_steps import parse_opt_step


class AliasTests(unittest.TestCase):
    def test_cli_aliases_select_the_same_sdk_step_as_numbers(self):
        for step, alias in ((0,"OM"),(9,"DO"),(13,"DW"),(16,"SW"),(17,"LC")):
            for spelling in (str(step), alias, alias.lower()):
                args = capture.base.parse_args(["--opt-step", spelling])
                self.assertEqual(args.opt_step, step)
        for bad in ("18.5", "unknown", "27", "-1"):
            with self.assertRaises(argparse.ArgumentTypeError):
                parse_opt_step(bad)
        self.assertEqual(parse_opt_step("26"), 26)

    def test_current_capture_rejects_the_previous_dataflow(self):
        old = dict(layer=capture.DEFAULTS.layer, seed=capture.DEFAULTS.seed,
            repeats=capture.DEFAULTS.repeats, opt_step=26, all_experts=False,
            estimate_scale=capture.DEFAULTS.estimate_scale, plan_only=False,
            sdk_commit=capture.prep.SDK_COMMIT, cases={"decode_b8_kv1024":{}})
        with self.assertRaisesRegex(ValueError, "fixed experiment settings"):
            capture.check_workload(old)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="layout-pipeline-", dir=ROOT / "tmp")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.patches = [mock.patch.object(parallel, "HERE", self.folder),
                        mock.patch.object(parallel, "git_revision", return_value="test-commit")]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.args = parallel.parse_args(["--campaign", "test"])
        self.args.poll_seconds = .02
        self.pipeline = parallel.Pipeline(self.args)
        self.addCleanup(self.pipeline.lock.close)

    def test_parallel_jobs_overlap_and_publish_finished_states(self):
        jobs = []
        for index in range(2):
            target = self.folder / f"job{index}.json"
            code = ("import json,time; from pathlib import Path; "
                    "start=time.monotonic(); time.sleep(.25); "
                    f"Path({str(target)!r}).write_text(json.dumps([start,time.monotonic()]))")
            jobs.append((f"job{index}", ["-c", code]))
        self.pipeline.group("probe", jobs, workers=2)
        windows = [json.loads((self.folder / f"job{i}.json").read_text()) for i in range(2)]
        self.assertLess(max(w[0] for w in windows), min(w[1] for w in windows))
        progress = json.loads((self.pipeline.folder / "progress.json").read_text())
        self.assertEqual([job["state"] for job in progress["jobs"]], ["complete", "complete"])
        self.assertTrue((self.pipeline.folder / "progress.md").is_file())
        self.assertEqual(progress["configuration"]["opt_alias"], "SW")

    def test_failure_prevents_queued_jobs_from_starting(self):
        marker = self.folder / "must-not-exist"
        with self.assertRaisesRegex(RuntimeError, "failed jobs: broken"):
            self.pipeline.group("failure", [("broken", ["-c", "raise SystemExit(3)"]),
                ("queued", ["-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"])])
        self.assertFalse(marker.exists())
        progress = json.loads((self.pipeline.folder / "progress.json").read_text())
        self.assertEqual([job["state"] for job in progress["jobs"]], ["failed", "skipped"])

    def test_second_controller_cannot_own_the_campaign(self):
        with self.assertRaises(BlockingIOError):
            parallel.Pipeline(self.args)


class ReplayGuardTests(unittest.TestCase):
    def test_runtime_cooling_must_match_the_campaign(self):
        with tempfile.TemporaryDirectory(prefix="layout-replay-", dir=ROOT / "tmp") as folder:
            folder = Path(folder)
            study = dict(thermal_step_ps=20_000_000, interval_ps=20_000_000,
                server_sha256="solver", heat_transfer_coefficient_w_m2_k=25000.)
            entry = dict(layer_start_ps=0, period_ps=10_000_000_000, recording_sha256={})
            summary = dict(status="time_limit", profile="constant", slots=50000,
                simulated_seconds=1., time_cap_seconds=1., allow_model_clamp=True,
                step_ps=20_000_000, slot_ps=20_000_000, cycles_completed=100,
                period_ps=10_000_000_000, layer_start_ps=0,
                layers_completed=100, factorization_threads=1, server_sha256="solver", recording_sha256={})
            write_json(folder / "summary.json", summary)
            (folder / "ice.stk").write_text("top heat sink: heat transfer coefficient 2.5e-8;\n")
            check_complete(folder, study, entry, "constant")
            with self.assertRaisesRegex(ValueError, "cooling coefficient"):
                check_complete(folder, dict(study, heat_transfer_coefficient_w_m2_k=35000.), entry, "constant")
            write_json(folder / "summary.json", dict(summary, allow_model_clamp=False))
            with self.assertRaisesRegex(ValueError, "allow_model_clamp"):
                check_complete(folder, study, entry, "constant")


if __name__ == "__main__":
    unittest.main()
