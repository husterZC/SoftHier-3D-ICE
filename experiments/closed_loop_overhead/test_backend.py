"""Backend selection, CG timing, and compatibility of overhead measurements."""

import copy
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from . import backend, runner, replay_overhead
from .analyze import summarize_events
from .replay_analysis import summarize_capture, summarize_replay, combine
from .test_replay_overhead import NS, SLOT, event, capture_events, replay_fixture


def cg_backend():
    directory = backend.ROOT / "3D-ICE_30_Oct"
    return dict(dice_dir=str(directory), build_target="cpu",
                server=str(directory / "bin/3D-ICE-Server-cpu"), client=str(directory / "bin/3D-ICE-Client-cpu"),
                solver_type=backend.CG, solver_threads=1, solver_tolerance=1e-6, blas_threads=1)


def cg_fixture():
    events, settings, summary = replay_fixture()
    for e in events:
        if e["phase"] == "factorization":
            e["phase"] = "preconditioner_setup"
    selected = cg_backend()
    settings["thermal_backend"] = selected
    summary.update({k: selected[k] for k in
                    ("server", "solver_type", "solver_threads", "solver_tolerance", "blas_threads")})
    summary["server_sha256"] = "saved-server-hash"
    return events, settings, summary


@unittest.skipUnless((backend.ROOT / "3D-ICE_30_Oct/Makefile").is_file(), "requires optional CPU source checkout")
class BackendTests(unittest.TestCase):
    def test_original_defaults_and_explicit_cpu_selection(self):
        original = runner.settings(runner.parse_args(["plan"]))
        self.assertEqual(original["thermal_backend"], backend.from_settings({}))
        args = runner.parse_args(["plan", "--dice-dir", str(backend.ROOT / "3D-ICE_30_Oct"),
                                  "--dice-build-target", "cpu", "--solver-type", backend.CG])
        config = runner.settings(args)
        self.assertEqual(config["thermal_backend"], cg_backend())
        self.assertEqual(config["thermal_threads"], 1)
        self.assertAlmostEqual(config["thermal_step_seconds"], 20e-6)
        command = runner.command_for(50., Path("/tmp/run"), SLOT, thermal_backend=config["thermal_backend"])
        for value in ("DICE_BUILD_TARGET=cpu", "ICE_SOLVER_TYPE=cpu_amgcl_cg_amg",
                      "ICE_SOLVER_THREADS=1", "ICE_SOLVER_TOLERANCE=1e-06"):
            self.assertIn(value, command)
        self.assertEqual(runner.command_for(None, Path("/tmp/run"), SLOT, thermal_backend=cg_backend()),
                         ["bash", str(runner.PROVIDER), "run-uncoupled"])

    def test_invalid_combinations_fail_before_a_run(self):
        for kwargs in (dict(solver=backend.CG), dict(tolerance=1e-6), dict(threads=0),
                       dict(directory=backend.ROOT / "3D-ICE_30_Oct"),
                       dict(directory=Path("/tmp")), dict(target="gpu")):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                backend.selection(**kwargs)
        for tolerance in (float("nan"), float("inf"), 0, -1):
            with self.assertRaises(ValueError):
                backend.selection(backend.ROOT / "3D-ICE_30_Oct", "cpu", backend.CG, 1, tolerance)

    def test_plan_is_non_mutating_and_source_snapshot_follows_selection(self):
        args = runner.parse_args(["plan", "--prefix", "unittest_backend_plan",
            "--dice-dir", str(backend.ROOT / "3D-ICE_30_Oct"), "--dice-build-target", "cpu",
            "--solver-type", backend.CG, "--solver-threads", "1", "--replicates", "1"])
        destination = runner.HERE / "results" / args.prefix
        self.assertFalse(destination.exists())
        stream = io.StringIO()
        with redirect_stdout(stream):
            runner.run(args)
        report = json.loads(stream.getvalue())
        self.assertEqual(len(report["schedule"]), 8)
        self.assertEqual(report["settings"]["thermal_backend"], cg_backend())
        self.assertFalse(destination.exists())
        sources = runner.source_files(cg_backend())
        self.assertIn(backend.ROOT / "3D-ICE_30_Oct/sources/cpu_amgcl_solver.cpp", sources)
        self.assertIn(backend.ROOT / "3D-ICE_30_Oct/makefile.def", sources)
        self.assertNotIn(backend.ROOT / "3D-ICE/bin/3D-ICE-Server.c", sources)
        self.assertNotIn(backend.ROOT / "3D-ICE_30_Oct/bison/stack_description_parser.c", sources)

    def test_replay_inherits_solver_and_preserves_one_capture(self):
        base = dict(case=runner.CASE, layer=1, seed=19, cpus=runner.select_cpus(),
                    interval_ps=SLOT, thermal_threads=1, thermal_backend=cg_backend())
        args = replay_overhead.parse_args(["plan", "--max-seconds", "1", "--replicates", "1"])
        settings = replay_overhead.settings_for(args, dict(manifest=dict(settings=base)))
        self.assertEqual(settings["thermal_backend"], base["thermal_backend"])
        self.assertIsNot(settings["thermal_backend"], base["thermal_backend"])
        self.assertEqual(settings["capture_count_per_workflow"], 1)
        command = replay_overhead.replay_command(Path("recording"), Path("replay"), settings)
        self.assertEqual(command[command.index("--server") + 1], cg_backend()["server"])
        self.assertEqual(command[command.index("--solver-type") + 1], backend.CG)
        self.assertEqual(command[command.index("--solver-threads") + 1], "1")
        self.assertEqual(command[command.index("--solver-tolerance") + 1], "1e-06")

    def test_effective_configuration_must_match_identity(self):
        selected = cg_backend()
        actual = dict(selected, server_sha256="expected")
        backend.verify_metadata(actual, selected, "expected")
        for field, value in (("solver_type", backend.LU), ("solver_threads", 4),
                             ("solver_tolerance", 1e-4), ("blas_threads", 2),
                             ("server", backend.from_settings({})["server"]),
                             ("server_sha256", "changed")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                backend.verify_metadata(dict(actual, **{field: value}), selected, "expected")


class ReferenceTests(unittest.TestCase):
    def test_reference_checks_selected_binary_stack_and_measurement(self):
        parent = backend.ROOT / "tmp/overhead_tests"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temporary:
            root = Path(temporary)
            fixture_dice = root / "fixture_dice"
            for relative in ("Makefile", "sources/cpu_amgcl_solver.cpp", "bin/3D-ICE-Server-cpu",
                             "bin/3D-ICE-Client-cpu"):
                path = fixture_dice / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n")
            selected = backend.selection(fixture_dice, "cpu", backend.CG, 1, 1e-6)
            source = root / "runs/block01_closed_loop_50"
            stack = source / "generated/3dice/ice.stk"
            stack.parent.mkdir(parents=True)
            stack.write_text("solver:\n transient step 2e-05, slot 2e-05;\n initial temperature 300;\n"
                             " type cpu_amgcl_cg_amg;\n numofcores 1;\n tolerance 1e-6;\n")
            (source / "generated/system_config.json").write_text("{}\n")
            actual = backend.thermal_solver.describe(stack, selected["server"])
            runner.write_json(source / "generated/thermal_solver.json", actual)
            row = dict(status="complete", validation=dict(status="PASS"), timing=dict(native_cells=67139),
                       mesh=dict(total_cells=67139), thermal_backend=actual)
            runner.write_json(source / "measurement.json", row)
            paths = {}
            for name in ("binary", "preload", "arch", "thermal_config", "dram_power_config"):
                p = root / name
                p.write_text("fixture\n")
                paths[name] = str(p)
            case = {key: paths[key] for key in ("binary", "preload")}
            case.update({key + "_sha256": runner.sha256(paths[key]) for key in ("binary", "preload")})
            workload = dict(paths, repeats=1, layer=1, seed=19, dramsys_path=str(root / "dram"),
                            cases={runner.CASE: case})
            runner.write_json(root / "workload_reference.json", workload)
            settings = dict(case=runner.CASE, profile="temperature_aware", power_accounting_timing=True,
                            coupling_mode="optimized", thermal_threads=1, thermal_backend=selected,
                            interval_ps=SLOT, thermal_step_seconds=2e-5, layer=1, seed=19)
            manifest = dict(status="complete", settings=settings, configurations=dict(closed_loop_50=50),
                            schedule=[dict(id="block01_closed_loop_50", pilot=False, configuration="closed_loop_50")],
                            native_artifact_sha256={str(Path(selected[k]).relative_to(backend.ROOT)):
                                                   runner.sha256(selected[k]) for k in ("server", "client")})
            manifest.update({key + "_sha256": case[key + "_sha256"] for key in ("binary", "preload")})
            runner.write_json(root / "study.json", manifest)
            with patch.object(runner, "compatible_workload"), patch.object(runner, "source_files", return_value=[]), \
                    patch.object(replay_overhead, "load_contract", return_value={}), \
                    patch.object(replay_overhead, "Layout", return_value=SimpleNamespace(inputs=[])):
                loaded = replay_overhead.load_reference(root)
                self.assertEqual(loaded["manifest"]["settings"]["thermal_backend"], selected)
                self.assertEqual(loaded["input_sha256"][selected["server"]], actual["server_sha256"])
                changed = copy.deepcopy(row)
                changed["thermal_backend"]["solver_threads"] = 4
                runner.write_json(source / "measurement.json", changed)
                with self.assertRaisesRegex(ValueError, "measurement solver"):
                    replay_overhead.load_reference(root)
                runner.write_json(source / "measurement.json", row)
                stack.write_text(stack.read_text().replace("numofcores 1", "numofcores 4"))
                with self.assertRaisesRegex(ValueError, "solver_threads"):
                    replay_overhead.load_reference(root)
                manifest["native_artifact_sha256"][str(Path(selected["server"]).relative_to(backend.ROOT))] = "wrong checksum"
                runner.write_json(root / "study.json", manifest)
                with self.assertRaisesRegex(ValueError, "native artifact changed"):
                    replay_overhead.load_reference(root)


class CGTimingTests(unittest.TestCase):
    def test_cg_setup_is_initialization_and_capture_counted_once(self):
        events, settings, summary = cg_fixture()
        replay = summarize_replay(events, 120 * NS, 170 * NS, summary, settings)
        self.assertEqual(replay["factorizations"], 0)
        self.assertEqual(replay["preconditioner_setups"], 1)
        self.assertEqual(replay["detail_seconds"]["preconditioner_setup"], 3.)
        self.assertEqual(replay["initialization_seconds"], 10.)
        self.assertAlmostEqual(sum(replay["phases_seconds"].values()), replay["loop_seconds"])
        capture = summarize_capture(capture_events(), 5 * NS, 100 * NS, SLOT)
        combined = combine(capture, replay, 0)
        self.assertEqual(combined["capture_count"], 1)
        self.assertAlmostEqual(combined["runtime_seconds"], 110.)
        self.assertAlmostEqual(combined["runtime_seconds"] + sum(combined["excluded_seconds"].values()),
                               combined["end_to_end_seconds"])

    def test_closed_loop_counts_setup_without_changing_the_partition(self):
        events, settings, _ = cg_fixture()
        events = copy.deepcopy(events)
        for e in events:
            if e["phase"] in ("replay_start", "replay_end"):
                e["phase"] = e["phase"].replace("replay", "roi")
        events.extend([event("exchange", 130, 141, 0, SLOT),
                       event("exchange", 145, 156, SLOT, SLOT), event("matrix_nnz", 125, 125, value=100000)])
        cg = summarize_events(events, 120 * NS, 170 * NS, True, SLOT, solver_type=backend.CG)
        self.assertEqual((cg["factorizations"], cg["preconditioner_setups"]), (0, 1))
        self.assertEqual(cg["phases_end_to_end"]["initialization"], 10.)
        for e in events:
            if e["phase"] == "preconditioner_setup":
                e["phase"] = "factorization"
        lu = summarize_events(events, 120 * NS, 170 * NS, True, SLOT)
        self.assertEqual((lu["factorizations"], lu["preconditioner_setups"]), (1, 0))
        self.assertEqual(cg["phases_end_to_end"], lu["phases_end_to_end"])

    def test_wrong_missing_duplicate_or_late_setup_fails(self):
        for change in ("wrong", "missing", "duplicate", "late"):
            events, settings, summary = cg_fixture()
            setup = next(e for e in events if e["phase"] == "preconditioner_setup")
            if change == "wrong":
                setup["phase"] = "factorization"
            elif change == "missing":
                events.remove(setup)
            elif change == "duplicate":
                events.append(dict(setup))
            else:
                setup["end_ns"] = 131 * NS
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "preconditioner_setup"):
                summarize_replay(events, 120 * NS, 170 * NS, summary, settings)

    def test_wrong_solver_threads_and_tolerance_fail(self):
        for key, value in (("solver_type", backend.LU), ("solver_threads", 4), ("solver_tolerance", 1e-4),
                           ("blas_threads", 2)):
            events, settings, summary = cg_fixture()
            summary[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                summarize_replay(events, 120 * NS, 170 * NS, summary, settings)

    def test_saved_legacy_result_shape_and_capture_guard(self):
        events, settings, summary = replay_fixture()
        legacy = summarize_replay(events, 120 * NS, 170 * NS, summary, settings)
        self.assertEqual(legacy["factorizations"], 1)
        self.assertNotIn("preconditioner_setups", legacy)
        self.assertNotIn("preconditioner_setup", legacy["detail_seconds"])
        with self.assertRaisesRegex(ValueError, "thermal solver"):
            summarize_capture(capture_events() + [event("preconditioner_setup", 6, 8)], 5 * NS, 100 * NS, SLOT)


if __name__ == "__main__":
    unittest.main()
