"""Regression checks for SDK staging, untimed preload, and paired study validation."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import analyze
import prepare_workload as preparation
import run_study


class DecodeStudyTests(unittest.TestCase):
    def test_cli_uses_one_twenty_microsecond_interval_for_all_batches(self):
        args = run_study.parse_args(["--llm_model", "gpt-oss-120b", "--decode_batch", "1", "8",
                                     "--floorplan", "square_bands"])
        self.assertEqual(args.interval_ps, 20_000_000)
        self.assertEqual(args.decode_batch, [1, 8])
        self.assertEqual(args.repeats, 1)
        self.assertFalse(hasattr(args, "attention_interval_ps"))

    def test_invalid_study_options(self):
        for args in (["--decode_batch", "1", "1"], ["--decode_batch", "0"],
                     ["--interval-ps", "0"], ["--repeats", "0"], ["--prefix", "../old"],
                     ["--estimate-scale", "nan"]):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    run_study.parse_args(args)
        with self.assertRaisesRegex(ValueError, "between 1 and 64"):
            preparation.validate_settings({"max_batch": 64}, [65], 1, 1.0)

    @unittest.skipUnless((preparation.SDK / "LLM_decode/gpt-oss-120b/main.c").is_file(), "requires LLM SDK")
    def test_repetitions_preserve_the_single_layer_and_accumulate_all_stages(self):
        source = (preparation.SDK / "LLM_decode/gpt-oss-120b/main.c").read_text()
        self.assertEqual(preparation.repeat_source(source, 1), source)
        repeated = preparation.repeat_source(source, 3)
        loop = repeated.index("for (unsigned study_repeat")
        self.assertIn("study_repeat < 3", repeated)
        self.assertIn("ticks[index] +=", repeated)
        self.assertLess(loop, repeated.index("norm(HBM_NORM1_OUT"))
        self.assertLess(repeated.index("combine(); phase(13);"), repeated.index('flex_print("STAGE ")'))
        self.assertEqual(repeated.count("flex_timer_start();"), 1)
        self.assertEqual(repeated.count("flex_timer_end();"), 1)
        with self.assertRaisesRegex(ValueError, "boundaries changed"):
            preparation.repeat_source(source.replace("combine(); phase(13);", "changed();"), 3)

    def test_private_hbm_mapping_does_not_modify_upstream(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source/dramsys_configs"
            (source / "addressmapping").mkdir(parents=True)
            (source / "simconfig").mkdir()
            mapping = source / "addressmapping/am_hbm4_emu_16Gb_pc_brc.json"
            mapping.write_text(json.dumps({"addressmapping": {
                "BYTE_BIT": [0, 1, 2], "COLUMN_BIT": [3, 4, 5], "ROW_BIT": [28, 29, 30]}}))
            config = source / "simconfig/example.json"
            config.write_text(json.dumps({"simconfig": {"StoreMode": "NoStorage", "DatabaseRecording": True}}))
            originals = [p.read_bytes() for p in (mapping, config)]
            destination = root / "private"
            hashes = preparation.prepare_dramsys(source.parent, destination)
            self.assertEqual([p.read_bytes() for p in (mapping, config)], originals)
            adapted = json.loads((destination / "dramsys_configs/addressmapping" / mapping.name).read_text())
            self.assertEqual(adapted["addressmapping"]["BYTE_BIT"], [0, 1])
            self.assertEqual(adapted["addressmapping"]["ROW_BIT"], [27, 28, 29])
            adapted_config = json.loads((destination / "dramsys_configs/simconfig/example.json").read_text())
            self.assertEqual(adapted_config["simconfig"], {"StoreMode": "Store", "DatabaseRecording": False})
            self.assertEqual(len(hashes), 2)

    @staticmethod
    def timing_log():
        return "\n".join(["Direct ELF preload complete (cycle: 0)"] * 17 +
            [f"STAGE stage{i} 10" for i in range(14)] +
            ["LAYER_CYCLES 140", "Execution period is 160 ns", "ROUTING distinct_experts 4 assignments 4", "DECODE_DONE"])

    def test_timing_requires_untimed_preload_and_complete_consistent_layer(self):
        model = {"experts_per_token": 4, "num_experts": 128}
        text = self.timing_log()
        result = run_study.parse_timing(text, 1, model, 16)
        self.assertEqual(result["layer_cycles"], 140)
        self.assertEqual(result["loader_count"], 17)
        for corrupt in (text.replace("cycle: 0", "cycle: 1", 1),
                        text.replace("STAGE stage0 10", "STAGE stage0 11"),
                        text.replace("DECODE_DONE", "DECODE_FAIL"),
                        text.replace("assignments 4", "assignments 8"),
                        text.replace("160 ns", "139 ns")):
            with self.subTest(corrupt=corrupt[-80:]), self.assertRaises(ValueError):
                run_study.parse_timing(corrupt, 1, model, 16)

    def test_analysis_rejects_partial_duplicate_or_mismatched_pairs(self):
        base = {"experiment": "llm_decode", "version": 1, "llm_model": "gpt-oss-120b",
                "decode_batches": [1], "repeats": 1, "interval_ps": 20_000_000,
                "target_cells": 16384, "estimate_scale": 1, "floorplan_rule": "square_bands", "sdk_commit": "test"}
        entries = [dict(llm_model=base["llm_model"], decode_batch=1, profile=p,
                        interval_ps=base["interval_ps"], repeats=1) for p in run_study.PROFILES]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "study.json"
            for runs in (entries[:1], entries + entries[:1], []):
                preparation.write_json(path, dict(base, runs=runs))
                with self.assertRaisesRegex(ValueError, "incomplete|duplicate"):
                    analyze.merge_manifests([path])
            preparation.write_json(path, dict(base, runs=entries))
            self.assertEqual(len(analyze.merge_manifests([path])["runs"]), 2)
            with self.assertRaisesRegex(ValueError, "duplicate"):
                analyze.merge_manifests([path, path])
            second = path.with_name("second.json")
            preparation.write_json(second, dict(base, runs=entries, floorplan_rule="redmule_strip"))
            with self.assertRaisesRegex(ValueError, "different"):
                analyze.merge_manifests([path, second])


if __name__ == "__main__":
    unittest.main()
