"""Mesh, attribution, statistics, and native transport acceptance checks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from .analyze import PHASES, analyze, block_statistics, partition, read_events
from .runner import command_for, configurations, execute, parse_args, schedule
from . import runner
from Interface_scripts.tests.test_memory_on_logic import contract, thermal_inputs, GENERATOR, SERVER, ROOT
from Interface_scripts.providers.softhier import export_system_config as exporter
from experiments.thermal_replay.plot_stack import read_stack


class MeshTests(unittest.TestCase):
    def generate(self, folder, document, *mesh):
        folder.mkdir(parents=True)
        (folder / "system.json").write_text(json.dumps(document))
        cmd = [sys.executable, str(GENERATOR), "--system-config", str(folder / "system.json"),
               "--geo", str(folder / "geo.json"), "--floorplan", str(folder / "floorplan.flp"),
               "--stk", str(folder / "ice.stk"), "--power-interval-ps", "20000000",
               "--step-seconds", "2e-5", "--map-output", "final", *mesh]
        return subprocess.run(cmd, text=True, capture_output=True, timeout=45)

    def test_actual_b8_counts_and_legacy_mesh(self):
        path = ROOT / "SoftHier/soft_hier_sdk/apps_dram3d/config/arch.py"
        document = exporter.build_contract(exporter.import_architecture(path), path, 0, floorplan_rule="square_bands")
        with tempfile.TemporaryDirectory() as temporary:
            for pitch, count in ((100, 18107), (50, 67139), (20, 396347)):
                folder = Path(temporary) / str(pitch)
                result = self.generate(folder, document, "--grid-pitch-um", str(pitch))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = json.loads((folder / "mesh.json").read_text())
                self.assertEqual(report["total_cells"], count)
                self.assertEqual(len(report["layers"]), 17)
                for layer in report["layers"]:
                    self.assertLessEqual(max(layer["dx_um"] + layer["dy_um"]), pitch + 1e-9)
                stack = read_stack(folder / "ice.stk")
                self.assertEqual(stack["total_thickness_um"], 1242)
                self.assertEqual(stack["step_seconds"], stack["slot_seconds"])
            folder = Path(temporary) / "legacy"
            result = self.generate(folder, document, "--target-top-die-cells", "16384")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((folder / "mesh.json").read_text())["total_cells"], 138960)

    def test_invalid_and_conflicting_mesh_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            for i, options in enumerate((("--grid-pitch-um", "nan"), ("--grid-pitch-um", "0"),
                    ("--grid-pitch-um", "50", "--target-top-die-cells", "32"))):
                result = self.generate(Path(temporary) / str(i), contract(1, 1), *options)
                self.assertNotEqual(result.returncode, 0)

    @unittest.skipUnless(SERVER.is_file(), "requires built 3D-ICE")
    def test_native_counts_steps_final_maps_and_instrumentation_invariance(self):
        with tempfile.TemporaryDirectory() as temporary:
            for order in ("dram_on_logic", "logic_on_dram"):
                saved = []
                for instrument in (False, True):
                    folder = Path(temporary) / f"{order}_{instrument}"
                    result = self.generate(folder, contract(1, 1, thermal_config=thermal_inputs(order)),
                                           "--grid-pitch-um", "20")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    manifest = json.loads((folder / "thermal_manifest.json").read_text())
                    width = len(manifest["power_order"])
                    (folder / "power.txt").write_text(" ".join(["0"] * width) + "\n" +
                        " ".join(["0.001"] * width) + "\n" + " ".join(["-1"] * width) + "\n")
                    env = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
                    env.pop("CLO_TIMING_DIR", None)
                    if instrument:
                        (folder / "timing").mkdir()
                        env["CLO_TIMING_DIR"] = str(folder / "timing")
                    run = subprocess.run([str(SERVER), "ice.stk", "--power-trace", "power.txt", "--until-minus-one"],
                                         cwd=folder, env=env, capture_output=True, text=True, timeout=60)
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                    saved.append([(folder / d["temperature_output"]).read_text() for d in manifest["dies"]])
                    for die in manifest["dies"]:
                        self.assertTrue((folder / die["map_output"]).stat().st_size > 0)
                    if instrument:
                        events = read_events(folder / "timing")
                        self.assertEqual(sum(e["phase"] == "thermal_step" for e in events), 2)
                        self.assertEqual(sum(e["phase"] == "factorization" for e in events), 1)
                        cells = next(e["value"] for e in events if e["phase"] == "mesh_cells")
                        self.assertEqual(cells, json.loads((folder / "mesh.json").read_text())["total_cells"])
                        self.assertTrue(any(e["phase"] == "process_exit" and e["maxrss_kb"] > 0 for e in events))
                self.assertEqual(saved[0], saved[1])


class TimingTests(unittest.TestCase):
    def test_nested_waits_are_not_double_counted(self):
        events = [dict(phase=name, start_ns=a, end_ns=b) for name, a, b in
                  (("exchange", 20, 60), ("power_capture", 20, 25),
                   ("thermal_step", 30, 50), ("thermal_output", 48, 55))]
        totals = partition(events, 0, 100, (10, 90))
        expected = dict(initialization=10, completion=10, architecture=40, power_capture=5,
                        thermal_advancement=20, coupling_output=15)
        for phase, value in expected.items():
            self.assertAlmostEqual(totals[phase], value * 1e-9)
        self.assertAlmostEqual(sum(totals.values()), 100e-9)

    def test_paired_ratio_and_failed_block_exclusion(self):
        modes = configurations([100, 50, 20])
        rows = [dict(configuration=name, block=b, pilot=False, status="complete",
                     timing=dict(end_to_end_seconds=(b + 1) * (i + 1), roi_seconds=(b + 2) * (i + 1)))
                for b in range(5) for i, name in enumerate(modes)]
        rows[-1]["status"] = "failed"
        stats, blocks = block_statistics(rows, modes, samples=200)
        self.assertEqual(len(blocks), 4)
        for s in stats:
            ratio = list(modes).index(s["configuration"]) + 1
            self.assertEqual(s["slowdown"], ratio)
            self.assertEqual(s["slowdown_ci95"], [ratio, ratio])

    def test_schedule_and_commands_preserve_four_modes_and_equal_steps(self):
        args = parse_args(["plan"])
        modes = configurations(args.grid_pitches_um)
        plan = schedule(modes, 19, 5)
        self.assertEqual(len(plan), 24)
        self.assertEqual(sum(not item["pilot"] for item in plan), 20)
        self.assertEqual(plan, schedule(modes, 19, 5))
        for block in range(6):
            self.assertEqual({i["configuration"] for i in plan if i["block"] == block}, set(modes))
        for pitch in modes.values():
            command = command_for(pitch, Path("/tmp/run"), 20_000_000)
            if pitch is None:
                self.assertEqual(command[-1], "run-uncoupled")
            else:
                self.assertIn("ICE_STEP_SECONDS=2e-05", command)
                self.assertIn("ICE_SLOT_SECONDS=2e-05", command)

    def test_native_headers_stay_identical(self):
        self.assertEqual((ROOT / "3D-ICE/include/closed_loop_timing.h").read_bytes(),
                         (ROOT / "Interface_scripts/providers/softhier/native/closed_loop_timing.h").read_bytes())

    def test_saved_results_generate_csv_and_publication_figures(self):
        modes = configurations([100, 50, 20])
        plan = schedule(modes, 19, 2)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "study.json").write_text(json.dumps(dict(schedule=plan, configurations=modes,
                settings=dict(seed=19, replicates=2))))
            for item in plan:
                scale = list(modes).index(item["configuration"]) + 1
                phases = dict.fromkeys(PHASES, 0.)
                phases["architecture"] = 10 * scale
                timing = dict(end_to_end_seconds=10 * scale, roi_seconds=10 * scale,
                              phases_end_to_end=phases, phases_roi=phases,
                              native_cells=scale * 100, matrix_nnz=scale * 500,
                              exchange_count=4, thermal_steps=4, factorizations=1,
                              solver_seconds_per_step=.01 * scale,
                              processes={"1": dict(actor="ice", maxrss_kb=1024 * scale)})
                path = folder / "runs" / item["id"]
                path.mkdir(parents=True)
                (path / "measurement.json").write_text(json.dumps(dict(item, status="complete", timing=timing)))
            result = analyze(folder)
            self.assertEqual(result["complete_blocks"], 2)
            self.assertFalse(result["missing_runs"])
            for name in ("runtime.pdf", "phases.pdf", "mesh_cost.pdf", "summary.csv", "runs.csv"):
                self.assertGreater((folder / name).stat().st_size, 100)

    def test_timeout_stops_child_and_preserves_cpu_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "timeout.log"
            with self.assertRaises(subprocess.TimeoutExpired):
                execute([sys.executable, "-c", "import time; time.sleep(60)"], dict(os.environ), log, .05)
            usage = json.loads(log.with_suffix(".resources.json").read_text())
            self.assertIsNotNone(usage["returncode"])
            self.assertGreaterEqual(usage["process_tree_user_seconds"], 0)

    def test_failed_study_resumes_without_repeating_completed_runs(self):
        # Fake external processes only; use real scheduling, event analysis,
        # persistence, failed-attempt retention, and resume validation.
        base = ROOT / "tmp/overhead_tests"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as temporary, ExitStack() as patches:
            folder = Path(temporary)
            args = parse_args(["run", "--prefix", "fixture", "--replicates", "1"])
            config, modes = runner.settings(args), configurations(args.grid_pitches_um)
            source = folder / "source"
            source.mkdir()
            environment = folder / "environment/dramsys_configs"
            environment.mkdir(parents=True)
            (environment / "hbm2-example.json").write_text('{"simulation":{"simconfig":"settings.json"}}')
            (environment / "settings.json").write_text('{"simconfig":{"PowerAnalysis":true}}')
            workload = dict(arch=str(folder / "arch.py"), source=str(source),
                thermal_config=str(CONFIGS_FOR_TEST / "dram3d_thermal.json"),
                dram_power_config=str(CONFIGS_FOR_TEST / "dram3d_power.json"),
                dramsys_path=str(environment.parent),
                cases={"decode_b8_kv1024": dict(binary="fixture.elf", preload="fixture-preload.elf",
                        binary_sha256="binary", preload_sha256="preload", manifest={})})
            artifacts = [folder / name for name in ("server", "native.json", "dram.json")]
            for artifact in artifacts:
                artifact.write_text("CLO_TIMING_DIR")
            calls, fail = [], [True]
            def fake_execute(command, env, log, timeout=None):
                if log.name in ("provider_check.log", "native_check.log"):
                    return 0, 1000
                directory = log.parent.parent
                calls.append(directory.name)
                if fail[0] and directory.name.startswith("block01"):
                    fail[0] = False
                    raise subprocess.TimeoutExpired(command, 1)
                thermal = "architecture_only" not in directory.name
                log.write_text("fixture")
                (log.parent / "simulator.log").write_text("fixture")
                log.with_suffix(".resources.json").write_text("{}")
                events = [dict(phase="roi_start", start_ns=100, end_ns=100, sim_ps=0, value=0),
                          dict(phase="roi_end", start_ns=900, end_ns=900, sim_ps=20_000_000, value=0)]
                if thermal:
                    events.extend(dict(phase=phase, start_ns=a, end_ns=b, sim_ps=20_000_000, value=value)
                                  for phase, a, b, value in (("exchange", 300, 700, 20_000_000),
                                  ("power_capture", 300, 320, 0), ("thermal_step", 350, 400, 1),
                                  ("thermal_solve", 360, 400, 0), ("factorization", 10, 20, 1),
                                  ("mesh_cells", 20, 20, 100), ("matrix_nnz", 20, 20, 500)))
                    mesh = directory / "generated/3dice"
                    mesh.mkdir(parents=True)
                    (mesh / "mesh.json").write_text('{"total_cells": 100}')
                for event in events:
                    event.update(actor="gvsoc", pid=1)
                (directory / "timing/1.jsonl").write_text("\n".join(map(json.dumps, events)) + "\n")
                return 0, 1000
            for name, value in (("source_hashes", lambda: {}), ("host_info", lambda: {}),
                                ("prepare", lambda *a, **k: workload), ("compatible_workload", lambda *a: None),
                                ("native_artifacts", lambda: artifacts), ("execute", fake_execute),
                                ("load_sdk_report", lambda *a: None),
                                ("parse_timing", lambda *a: dict(cycles=123, stage_cycles={"work": 123})),
                                ("effective_configuration", lambda *a: {}), ("analyze", lambda *a: None)):
                patches.enter_context(patch.object(runner, name, value))
            directory = folder / "study"
            with self.assertRaises(subprocess.TimeoutExpired):
                runner.run_locked(args, directory, config, modes)
            manifest = json.loads((directory / "study.json").read_text())
            self.assertEqual(manifest["status"], "failed")
            completed = calls[:-1]
            failed = calls[-1]
            args.resume = True
            runner.run_locked(args, directory, config, modes)
            self.assertEqual(json.loads((directory / "study.json").read_text())["status"], "complete")
            for name in completed:
                self.assertEqual(calls.count(name), 1)
            self.assertEqual(calls.count(failed), 2)
            self.assertEqual(len(list((directory / "runs").glob(failed + "_failed_*"))), 1)

    def test_baseline_only_changes_the_active_dram_configuration(self):
        base = ROOT / "tmp/overhead_tests"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as temporary:
            folder = Path(temporary)
            configs = folder / "source/dramsys_configs"
            configs.mkdir(parents=True)
            (configs / "hbm2-example.json").write_text('{"simulation":{"simconfig":"settings.json"}}')
            (configs / "settings.json").write_text('{"simconfig":{"PowerAnalysis":true,"StoreMode":"Store"}}')
            (configs / "unrelated.json").write_text('{"example": true, /* relaxed JSON */}')
            result = Path(runner.no_power_environment(configs.parent, folder / "baseline")) / "dramsys_configs"
            self.assertEqual(json.loads((result / "settings.json").read_text()),
                             {"simconfig": {"PowerAnalysis": False, "StoreMode": "Store"}})
            self.assertEqual((configs / "unrelated.json").read_bytes(), (result / "unrelated.json").read_bytes())
            self.assertTrue(json.loads((configs / "settings.json").read_text())["simconfig"]["PowerAnalysis"])


CONFIGS_FOR_TEST = ROOT / "Interface_scripts/providers/softhier/configs"


if __name__ == "__main__":
    unittest.main()
