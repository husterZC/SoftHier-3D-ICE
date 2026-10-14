"""Small fixtures for SDK staging, timing checks, die attribution and pairing."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import analyze
import prepare_workload as preparation
import run_study
import thermal_validation

try:
    import numpy as np
except ImportError:
    np = None

ROOT = preparation.ROOT
INSPECTED_SDK = (preparation.SDK if (preparation.SDK / "llm_dram3d").is_dir()
                 else ROOT / "tmp/dram3d_plan/softhier-sdk")


class DramStudyTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix="llm-dram3d-test-", dir=ROOT / "tmp")
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

    def test_cli_defaults_and_sdk_case_selection(self):
        args = run_study.parse_args([])
        self.assertEqual(args.cases, ["decode_b1_kv1024"])
        self.assertEqual(args.interval_ps, 20_000_000)
        self.assertEqual(args.layer, 1)
        self.assertEqual(args.seed, 19)
        self.assertEqual(args.repeats, 1)
        self.assertEqual(args.floorplan, "redmule_strip")
        self.assertEqual(args.thermal_config.name, "dram3d_thermal.json")
        args = run_study.parse_args(["--cases", "decode_b8_kv1024", "prefill_s128",
                                     "--repeats", "3", "--floorplan", "square_bands"])
        self.assertEqual(len(args.cases), 2)
        self.assertEqual(args.repeats, 3)
        self.assertEqual(args.floorplan, "square_bands")
        for options in (["--cases", "decode_b1_kv1024", "decode_b1_kv1024"],
                        ["--layer", "36"], ["--seed", "-1"], ["--interval-ps", "0"],
                        ["--prefix", "../old"], ["--estimate-scale", "nan"],
                        ["--repeats", "0"], ["--repeats", "-1"], ["--repeats", "1.5"],
                        ["--repeats", "4294967296"], ["--floorplan", "unknown"]):
            with self.subTest(options=options), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    run_study.parse_args(options)

    def test_invalid_repeats_fail_before_workload_output_exists(self):
        output = self.root / "workload"
        for repeats in (0, -1, True, 1.5, 1 << 32):
            with self.subTest(repeats=repeats), self.assertRaisesRegex(ValueError, "repeats"):
                preparation.prepare(INSPECTED_SDK, output, [preparation.CASES[0]], repeats=repeats)
            self.assertFalse(output.exists())

    @unittest.skipUnless((INSPECTED_SDK / "llm_dram3d/gpt-oss-120b/sw/main.c").is_file(), "requires pinned SDK")
    def test_repeat_staging_preserves_default_and_rejects_changed_boundaries(self):
        source = (INSPECTED_SDK / "llm_dram3d/gpt-oss-120b/sw/main.c").read_text()
        self.assertEqual(preparation.repeat_source(source, 1), source)
        for changed in (source.replace("STAGE(10, combine(cid));", "changed();"),
                        source.replace("elapsed += stages[id];", "elapsed += 1;"), source + source):
            with self.assertRaisesRegex(ValueError, "boundaries changed"):
                preparation.repeat_source(changed, 3)

    @unittest.skipUnless(shutil.which("cc") and
        (INSPECTED_SDK / "llm_dram3d/gpt-oss-120b/sw/main.c").is_file(), "requires C compiler and pinned SDK")
    def test_repeated_main_executes_all_stages_and_keeps_64_bit_totals(self):
        # Execute the actual staged main with host stubs. Deliberately wrap the
        # cycle register and exceed 32 bits in every accumulated stage counter.
        source = (INSPECTED_SDK / "llm_dram3d/gpt-oss-120b/sw/main.c").read_text()
        main = preparation.repeat_source(source, 3).split("int main(void)", 1)[1]
        constants = sorted(set(re.findall(r"\bL_[A-Z_0-9]+\b", main)) -
                           {"L_BATCH", "L_SEQ", "L_PAST", "L_LAYER"})
        prefix = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#define NC 1
#define L_BATCH 1
#define L_SEQ 1
#define L_PAST 1024
#define L_LAYER 1
static unsigned sync_words[16], operations, starts, ends, checks;
#define ARCH_SYNC_BASE ((uintptr_t)sync_words)
#define ARCH_SYNC_SIZE 64
#define flex_get_cluster_id() 0u
#define flex_is_first_core() 1u
#define flex_barrier_xy_init() ((void)0)
#define flex_global_barrier_xy() ((void)0)
#define flex_timer_start() (++starts)
#define flex_timer_end() (++ends)
static unsigned cycles(void) {
    static unsigned now = 0xfffffff0u;
    unsigned previous = now; now += 0x80000007u; return previous;
}
static void operation(unsigned stage) {
    assert(starts == 1 && ends == 0);
    assert(stage == operations % 11); ++operations;
}
#define norm(cid, in, out, w) operation((in) == L_OFF_X ? 0 : 6)
#define projection(cid, in, out, k, n, w, b) \
    operation((out) == L_OFF_QKV ? 1 : ((out) == L_OFF_PROJECTED ? 4 : 7))
#define rope_append(cid) operation(2)
#define attention(cid) operation(3)
#define residual(cid) operation(5)
#define route(cid) operation(8)
#define experts(cid) operation(9)
#define combine(cid) operation(10)
static unsigned fixture_check(unsigned *checked) {
    assert(operations == 33 && ends == 1); ++checks; *checked = 10; return 0;
}
#define check(cid, checked) fixture_check(checked)
static void flex_eoc(unsigned error) {
    assert(error == 0 && starts == 1 && ends == 1 && checks == 1 && operations == 33);
}
'''
        fixture = self.root / "repeated_main.c"
        fixture.write_text(prefix + "\nenum { " + ", ".join(constants) + " };\nint main(void)" + main)
        binary = self.root / "repeated_main"
        subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", str(fixture), "-o", str(binary)],
                       env=dict(os.environ, TMPDIR=str(self.root)), check=True, capture_output=True, text=True)
        log = subprocess.check_output([str(binary)], text=True)
        log = "Direct ELF preload complete (cycle: 0)\n" * 2 + log
        report = preparation.load_sdk_report(INSPECTED_SDK / "llm_dram3d/gpt-oss-120b")
        manifest = dict(batch=1, sequence=1, past_kv=1024, layer=1, verified_elements=10)
        timing = run_study.parse_timing(log, manifest, report, 1, repeats=3)
        self.assertEqual(timing["cycles"], 33 * 0x80000007)
        self.assertEqual(set(timing["stage_cycles"].values()), {3 * 0x80000007})
        self.assertEqual(timing["repeats"], 3)
        self.assertEqual(timing["mean_layer_time_us"], timing["layer_time_us"] / 3)
        for corrupt in (log.replace("count=3", "count=2"), log.replace("count=3", "count=3\nLLM_DRAM3D_REPEATS count=3"),
                        log.replace("LLM_DRAM3D_REPEATS count=3", ""),
                        log.replace("cycles_hi=1", "cycles_hi=4294967296", 1),
                        log.replace("LLM_STAGE id=0", "LLM_STAGE id=1", 1),
                        log + "\nLLM_STAGE id=0 cycles=10", log.replace("checked=10", "checked=9")):
            with self.subTest(tail=corrupt[-80:]), self.assertRaises(ValueError):
                run_study.parse_timing(corrupt, manifest, report, 1, repeats=3)
        with self.assertRaisesRegex(ValueError, "repeat count"):
            run_study.parse_timing(log, manifest, report, 1, repeats=1)

    def test_private_hbm2_mapping_is_unchanged_and_power_enabled(self):
        source = self.root / "upstream/dramsys_configs"
        (source / "addressmapping").mkdir(parents=True)
        (source / "simconfig").mkdir()
        mapping = source / "addressmapping/native.json"
        mapping.write_text(json.dumps({"addressmapping": {"BYTE_BIT": [0, 1, 2, 3, 4],
                                                         "COLUMN_BIT": [5, 6], "ROW_BIT": [27, 28]}}))
        simulation = dict(addressmapping="addressmapping/native.json", simconfig="simconfig/example.json")
        (source / "hbm2-example.json").write_text(json.dumps(dict(simulation=simulation)))
        config = source / "simconfig/example.json"
        config.write_text(json.dumps(dict(simconfig=dict(StoreMode="NoStorage", PowerAnalysis=False,
                                                        DatabaseRecording=True, EnableWindowing=True))))
        originals = mapping.read_bytes(), config.read_bytes()
        destination = self.root / "private"
        hashes = preparation.prepare_dramsys(source.parent, destination)
        self.assertEqual((mapping.read_bytes(), config.read_bytes()), originals)
        self.assertEqual((destination / "dramsys_configs/addressmapping/native.json").read_bytes(), originals[0])
        self.assertEqual((destination / "dramsys_configs/simconfig/example.json").read_bytes(), originals[1])
        channel = json.loads((destination / "dramsys_configs/hbm2-example.json").read_text())
        self.assertEqual(channel["simulation"]["simconfig"], "simconfig/softhier_dram3d_power.json")
        actual = json.loads((destination / "dramsys_configs/simconfig/softhier_dram3d_power.json").read_text())["simconfig"]
        self.assertEqual(actual, dict(StoreMode="Store", PowerAnalysis=True,
                                     DatabaseRecording=False, EnableWindowing=False))
        self.assertEqual(len(hashes), 4)

    def test_invalid_inputs_fail_before_workload_output_exists(self):
        thermal = self.root / "invalid_thermal.json"
        thermal.write_text("{}")
        output = self.root / "workload"
        with self.assertRaises(ValueError):
            preparation.prepare(INSPECTED_SDK, output, [preparation.CASES[0]], thermal,
                                preparation.CONFIGS / "dram3d_power.json")
        self.assertFalse(output.exists())
        power = json.loads((preparation.CONFIGS / "dram3d_power.json").read_text())
        power["synthetic"] = True
        fixture = self.root / "synthetic_power.json"
        preparation.write_json(fixture, power)
        with self.assertRaisesRegex(ValueError, "synthetic"):
            preparation.validate_inputs(preparation.CONFIGS / "dram3d_thermal.json", fixture)

    def test_native_source_snapshot_covers_provider_build_helpers(self):
        provider = self.root / "provider"
        (provider / "native").mkdir(parents=True)
        (provider / "native/backend.cpp").write_text("backend fixture\n")
        helper = provider / "systemc_install.py"
        helper.write_text("build helper fixture\n")
        snapshot = self.root / "snapshot"
        hashes = preparation.snapshot_native_sources(snapshot, provider)
        self.assertEqual(hashes, preparation.native_source_hashes(provider))
        self.assertEqual(set(hashes), {"backend.cpp", "provider_helpers/systemc_install.py"})
        self.assertEqual((snapshot / "provider_helpers/systemc_install.py").read_bytes(), helper.read_bytes())
        helper.write_text("changed helper fixture\n")
        self.assertNotEqual(hashes, preparation.native_source_hashes(provider))

    @unittest.skipIf(np is None, "requires analysis NumPy dependency")
    def test_thermal_guard_checks_feedback_floorplan_and_every_cell_row(self):
        folder = self.root / "results/3dice"
        folder.mkdir(parents=True)
        (folder / "ice.stk").write_text("top heat sink :\n heat transfer coefficient 1.5e-8 ;\n"
                                       " temperature 300 ;\nsolver:\n initial temperature 300 ;")
        contract = dict(contract=dict(version=2), thermal_feedback=dict(initial_temperature_c=26.85),
                        thermal_stack=dict(cooling=dict(temperature_k=300.)))
        die = dict(id="LOGIC_DIE", temperature_output="logic.flp", map_output="logic.map",
                   coordinates_output="logic.xy")
        manifest = dict(dies=[die])
        run = SimpleNamespace(path=self.root, temperatures=[dict(temperature_c=27., path="/chip/core")],
                              power_records=[{}])
        rows = [dict(values=[300., 301.])]
        common = SimpleNamespace(np=np, VIEW=SimpleNamespace(
            load_tflp_rows=lambda path: (None, rows),
            load_tmap_geometry_cells=lambda path: dict(cell_count=2)))
        mapfile = folder / "logic.map"
        mapfile.write_text("% synthetic thermal fixture\n300 301\n299.9995 302\n")
        result = analyze.validate_thermal_physics(common, run, contract, manifest)
        self.assertEqual(result["bounds"]["lower_bound_k"], 300.)
        self.assertEqual(result["cell_statistics"]["LOGIC_DIE"], (2, 299.9995, 302.))
        for row in ("285.8 302", "nan 302", "300 inf", "300 301 invalid", "300"):
            mapfile.write_text("300 301\n" + row + "\n")
            with self.subTest(raw_map=row), self.assertRaises(ValueError):
                analyze.validate_thermal_physics(common, run, contract, manifest)
        mapfile.write_text("300 301\n")
        rows[0]["values"][0] = 285.8
        with self.assertRaisesRegex(ValueError, "floorplan.*lower bound"):
            analyze.validate_thermal_physics(common, run, contract, manifest)
        rows[0]["values"][0] = 300.
        run.temperatures[0]["temperature_c"] = 12.
        with self.assertRaisesRegex(ValueError, "feedback.*lower bound"):
            analyze.validate_thermal_physics(common, run, contract, manifest)

    def test_thermal_lower_bound_accounts_for_cooler_initial_state(self):
        contract = dict(contract=dict(version=2), thermal_feedback=dict(initial_temperature_c=21.85),
                        thermal_stack=dict(cooling=dict(temperature_k=300.)))
        stack = "top heat sink : heat transfer coefficient 1.5e-8 ; temperature 300 ; initial temperature 295 ;"
        bounds = thermal_validation.thermal_bounds(contract, stack)
        self.assertEqual(bounds["lower_bound_k"], 295.)
        thermal_validation.validate_temperature(295., bounds, "initial state")
        with self.assertRaisesRegex(ValueError, "initial temperature differs"):
            thermal_validation.thermal_bounds(contract, stack.replace("temperature 295", "temperature 290"))

    @unittest.skipUnless((INSPECTED_SDK / "utilities/config.py").is_file(), "requires pinned SDK")
    def test_simulator_only_power_scale_does_not_enter_assembly_headers(self):
        arch = self.root / "arch.py"
        preparation.stage_architecture(INSPECTED_SDK / "apps_dram3d/config/arch.py", arch, 1.25)
        configured = runpy.run_path(str(arch))["FlexClusterArch"]()
        self.assertEqual(configured.power_estimate_scale, 1.25)
        self.assertEqual(configured.preload_mode, "direct")
        subprocess.run([sys.executable, str(INSPECTED_SDK / "utilities/config.py"), str(arch),
                        "--output-dir", str(self.root)], check=True, stdout=subprocess.PIPE, text=True)
        header = (self.root / "flex_cluster_arch.inc").read_text()
        self.assertIn("ARCH_DRAM3D_ADDR_BASE", header)
        self.assertNotIn("ARCH_POWER_ESTIMATE_SCALE", header)
        self.assertNotIn("ARCH_PRELOAD_MODE", header)

    @unittest.skipUnless((INSPECTED_SDK / "llm_dram3d/gpt-oss-120b/scripts/run.py").is_file(),
                         "requires the inspected pinned SDK checkout")
    def test_sdk_timing_checks_exact_scenario_all_stages_and_untimed_preload(self):
        report = preparation.load_sdk_report(INSPECTED_SDK / "llm_dram3d/gpt-oss-120b")
        manifest = dict(batch=1, sequence=1, past_kv=1024, layer=1, verified_elements=10)
        text = "\n".join(["Direct ELF preload complete (cycle: 0)"] * 17 +
            [f"LLM_STAGE id={index} cycles=10" for index in range(11)] +
            ["LLM_DRAM3D_PASS batch=1 seq=1 past=1024 layer=1 cycles_hi=0 cycles_lo=110 errors=0 checked=10"])
        timing = run_study.parse_timing(text, manifest, report, 16)
        self.assertEqual(timing["cycles"], 110)
        self.assertEqual(len(timing["stage_cycles"]), 11)
        self.assertEqual(timing["preload_cycles"], 0)
        self.assertEqual(timing["repeats"], 1)
        self.assertEqual(timing["mean_layer_time_us"], timing["layer_time_us"])
        with self.assertRaisesRegex(ValueError, "repeat count"):
            run_study.parse_timing(text, manifest, report, 16, repeats=2)
        for corrupt in (text.replace("cycle: 0", "cycle: 1", 1), text.replace("id=0 cycles=10", "id=0 cycles=11"),
                        text.replace("checked=10", "checked=9"), text.replace("past=1024", "past=2048"),
                        text + "\nMISMATCH", text + "\nLLM_DRAM3D_PASS"):
            with self.subTest(tail=corrupt[-80:]), self.assertRaises(ValueError):
                run_study.parse_timing(corrupt, manifest, report, 16)

    def test_merge_rejects_partial_duplicate_or_different_models(self):
        base = dict(experiment="llm_dram3d", version=1, cases=[preparation.CASES[0]], interval_ps=20_000_000,
                    thermal_config_sha256="thermal", dram_power_config_sha256="electrical")
        entries = [dict(case=preparation.CASES[0], profile=profile, interval_ps=20_000_000)
                   for profile in run_study.PROFILES]
        path = self.root / "study.json"
        for runs in ([], entries[:1], entries + entries[:1]):
            preparation.write_json(path, dict(base, runs=runs))
            with self.assertRaisesRegex(ValueError, "incomplete|duplicate"):
                analyze.merge_manifests([path])
        preparation.write_json(path, dict(base, runs=entries))
        merged = analyze.merge_manifests([path])
        self.assertEqual(len(merged["runs"]), 2)
        self.assertEqual(merged["repeats"], 1)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            analyze.merge_manifests([path, path])
        other = self.root / "other.json"
        preparation.write_json(other, dict(base, runs=entries, dram_power_config_sha256="different"))
        with self.assertRaisesRegex(ValueError, "different"):
            analyze.merge_manifests([path, other])
        preparation.write_json(other, dict(base, runs=entries, repeats=2))
        with self.assertRaisesRegex(ValueError, "different"):
            analyze.merge_manifests([path, other])
        with self.assertRaisesRegex(ValueError, "run repeats differ"):
            analyze.merge_manifests([other])
        repeated_entries = [dict(entry, repeats=2) for entry in entries]
        preparation.write_json(other, dict(base, runs=repeated_entries, repeats=2))
        self.assertEqual(analyze.merge_manifests([other])["repeats"], 2)

    @staticmethod
    def synthetic_pair():
        paths = {"/chip/core": "LOGIC_DIE", "/chip/dram3d_chan_0": "DRAM_DIE"}
        contract = dict(thermal_feedback=dict(components=[dict(path=p, die=d) for p, d in paths.items()]))
        metadata = dict(components=[dict(simulator_path=p, kind="core" if d == "LOGIC_DIE" else "dram3d_channel")
                                    for p, d in paths.items()])
        records = [dict(window=dict(start_ps=start, end_ps=end), components=[
            dict(path="/chip/core", dynamic_w=2., leakage_w=.1),
            dict(path="/chip/dram3d_chan_0", dynamic_w=1., leakage_w=bg)])
                   for start, end, bg in ((0, 20_000_000, .2), (20_000_000, 25_000_000, .4))]
        constant = SimpleNamespace(profile="constant", metadata=metadata, power_records=records)
        aware = copy.deepcopy(constant)
        aware.profile = "temperature_aware"
        aware.power_records[1]["components"][0]["leakage_w"] = .11
        return constant, aware, [contract, copy.deepcopy(contract)]

    def test_dram_background_can_change_with_state_but_must_match_pair(self):
        constant, aware, contracts = self.synthetic_pair()
        self.assertEqual(analyze.validate_pair(constant, aware, contracts), 0.)
        aware.power_records[1]["components"][1]["leakage_w"] = .41
        with self.assertRaisesRegex(ValueError, "leakage_w differs"):
            analyze.validate_pair(constant, aware, contracts)

    def test_energy_uses_actual_final_interval_and_separate_dram_group(self):
        constant, _, contracts = self.synthetic_pair()
        energy = analyze.grouped_energy(constant, analyze.die_paths(contracts[0]))
        self.assertAlmostEqual(energy["LOGIC_DIE", "core"]["dynamic_j"], 50e-6)
        self.assertAlmostEqual(energy["DRAM_DIE", "dram"]["dynamic_j"], 25e-6)
        self.assertAlmostEqual(energy["DRAM_DIE", "dram"]["leakage_j"], 6e-6)

    def test_legacy_logic_coefficient_check_sees_no_dram_components(self):
        constant, _, contracts = self.synthetic_pair()
        common = SimpleNamespace(validate_new_logic_leakage=mock.Mock(return_value=0.))
        analyze.validate_logic_leakage(common, constant, analyze.die_paths(contracts[0]))
        checked = common.validate_new_logic_leakage.call_args.args[0]
        self.assertEqual([item["kind"] for item in checked.metadata["components"]], ["core"])

    def test_outside_paths_are_rejected_without_opening_them(self):
        with self.assertRaisesRegex(ValueError, "inside this repository"):
            preparation.within_repo(ROOT.parent / "outside")


if __name__ == "__main__":
    unittest.main()
