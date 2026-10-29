"""Protect the scientific accounting and sample selection used by the figure."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plot_closed_loop_overhead import (  # noqa: E402
    PHASES, SCOPES, exclusive_phases, select_blocks, summarize,
)
from experiments.closed_loop_overhead.analyze import power_accounting_counter


def measurement(name, block=1, *, roi=100., total=120., pilot=False, status="complete"):
    return dict(id=f"block{block}_{name}", configuration=name, block=block, pilot=pilot, status=status,
                timing=dict(roi_seconds=roi, end_to_end_seconds=total, native_cells=0 if name == "architecture_only" else 100))


class SelectionTests(unittest.TestCase):
    def test_pilots_failed_and_incomplete_blocks_are_excluded(self):
        names = ("architecture_only", "closed_loop_100")
        rows = [measurement(n) for n in names]
        rows += [measurement(n, block=0, pilot=True, roi=.1) for n in names]
        rows += [measurement(names[0], block=2)]
        rows += [measurement(names[0], block=3), measurement(names[1], block=3, status="failed")]
        blocks = select_blocks(rows, names)
        self.assertEqual(len(blocks), 1)
        self.assertEqual({r["block"] for r in blocks[0].values()}, {1})

    def test_duplicate_case_and_no_complete_block_are_rejected(self):
        names = ("architecture_only", "closed_loop_100")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            select_blocks([measurement(names[0]), measurement(names[0])], names)
        with self.assertRaisesRegex(ValueError, "no complete"):
            select_blocks([measurement(names[0]), measurement(names[1], status="failed")], names)


class AccountingTests(unittest.TestCase):
    def test_mapping_is_exclusive_of_capture_and_solver_and_does_not_mutate_input(self):
        def event(phase, start, end, sim_ps=0):
            return dict(phase=phase, start_ns=int(start * 1e9), end_ns=int(end * 1e9), sim_ps=sim_ps, pid=7)
        events = [event("roi_start", 10, 10, 10000), event("roi_end", 90, 90, 90000),
                  event("exchange", 20, 80), event("power_capture", 20, 30),
                  event("power_mapping", 25, 40), event("thermal_step", 35, 50),
                  event("thermal_output", 45, 55)]
        events.append(dict(phase="inline_power_accounting", kind="counter", method_version=1, scope="roi",
            pid=7, start_ns=90_000_000_000, end_ns=90_000_000_000, sim_ps=90000, value=8_000_000_000,
            calls=10, window_start_ns=10_000_000_000, window_end_ns=90_000_000_000,
            window_start_ps=10000, window_end_ps=90000, dram_callbacks_registered=True))
        original = copy.deepcopy(events)
        row = measurement("closed_loop_100", roi=80., total=100.)
        row.update(start_ns=0, end_ns=100_000_000_000)
        row["timing"]["roi_simulated_ps"] = 80000
        row["timing"]["power_accounting"] = power_accounting_counter(events, (10_000_000_000, 90_000_000_000))
        result = exclusive_phases(row, events)
        roi = result[SCOPES[0]]
        self.assertAlmostEqual(roi["power_estimation"], 23.)
        self.assertAlmostEqual(roi["thermal_advancement"], 15.)
        self.assertAlmostEqual(roi["coupling_output"], 30.)
        self.assertAlmostEqual(roi["architecture"], 12.)
        self.assertAlmostEqual(sum(roi.values()), 80.)
        self.assertEqual(set(result), {"roi_seconds", "power_components_seconds"})
        self.assertAlmostEqual(result["power_components_seconds"]["inline_power_accounting"], 8.)
        self.assertAlmostEqual(result["power_components_seconds"]["power_capture_mapping"], 15.)
        self.assertNotIn("initialization", roi)
        self.assertNotIn("completion", roi)
        self.assertEqual(events, original)
        row["timing"]["roi_seconds"] = 70.
        with self.assertRaisesRegex(ValueError, "does not reconcile"):
            exclusive_phases(row, events)

    def summary(self, blocks):
        names = ("architecture_only", "closed_loop_100")
        phases = {}
        for block in blocks:
            for row in block.values():
                phases[row["id"]] = {scope: dict.fromkeys(PHASES, 0.) | {"architecture": row["timing"][scope]}
                                     for scope in SCOPES}
                phases[row["id"]]["power_components_seconds"] = dict(inline_power_accounting=0., power_capture_mapping=0.)
        return summarize(blocks, names, dict(architecture_only=None, closed_loop_100=100), phases, seed=19)

    def test_single_repeat_has_no_invented_intervals_and_uses_workload_baseline(self):
        blocks = [dict(architecture_only=measurement("architecture_only", roi=100., total=120.),
                       closed_loop_100=measurement("closed_loop_100", roi=150., total=160.))]
        records = self.summary(blocks)
        self.assertEqual({row["scope"] for row in records}, {"roi_seconds"})
        for row in records:
            self.assertIsNone(row["runtime_ci95_seconds"])
            self.assertIsNone(row["overhead_ci95_percent"])
            self.assertEqual(row["complete_blocks"], 1)
            if row["configuration"] == "architecture_only":
                self.assertEqual(row["overhead_percent"], 0)
            else:
                self.assertAlmostEqual(row["overhead_percent"], 50.)

    def test_multiple_blocks_use_ratio_of_means_and_paired_intervals(self):
        blocks = [dict(architecture_only=measurement("architecture_only", roi=100., total=120.),
                       closed_loop_100=measurement("closed_loop_100", roi=150., total=160.)),
                  dict(architecture_only=measurement("architecture_only", block=2, roi=200., total=300.),
                       closed_loop_100=measurement("closed_loop_100", block=2, roi=220., total=360.))]
        records = self.summary(blocks)
        row = next(r for r in records if r["configuration"] == "closed_loop_100" and r["scope"] == SCOPES[0])
        self.assertAlmostEqual(row["slowdown"], 370 / 300)
        self.assertAlmostEqual(row["overhead_ci95_percent"][0], 10.)
        self.assertAlmostEqual(row["overhead_ci95_percent"][1], 50.)
        for row in records:
            self.assertEqual(row["complete_blocks"], 2)
            self.assertIsNotNone(row["runtime_ci95_seconds"])
            if row["configuration"] == "architecture_only":
                self.assertEqual(row["overhead_ci95_percent"], [0., 0.])


if __name__ == "__main__":
    unittest.main()
