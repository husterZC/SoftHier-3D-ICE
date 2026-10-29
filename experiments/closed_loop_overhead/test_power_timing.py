"""Exclusive scalar attribution and always-on experiment settings."""
import copy
import unittest

from .analyze import partition, power_accounting_counter
from .runner import parse_args


def counter(value=15):
    return dict(phase="inline_power_accounting", kind="counter", method_version=1, scope="roi",
                pid=7, start_ns=90, end_ns=90, sim_ps=1000, value=value, calls=3,
                window_start_ns=10, window_end_ns=90, window_start_ps=0, window_end_ps=1000,
                dram_callbacks_registered=True)


class PowerTimingTests(unittest.TestCase):
    def test_timing_is_always_enabled(self):
        for command in ("plan", "run"):
            self.assertTrue(parse_args([command]).power_accounting_timing)
            self.assertTrue(parse_args([command, "--power-accounting-timing"]).power_accounting_timing)

    def test_counter_moves_time_out_of_architecture_once(self):
        events = [counter(), dict(phase="exchange", start_ns=20, end_ns=60),
                  dict(phase="power_capture", start_ns=20, end_ns=25),
                  dict(phase="thermal_step", start_ns=30, end_ns=50)]
        original = copy.deepcopy(events)
        phases = partition(events, 0, 100, (10, 90))
        self.assertAlmostEqual(phases["architecture"], 25e-9)
        self.assertAlmostEqual(phases["inline_power_accounting"], 15e-9)
        self.assertAlmostEqual(phases["power_capture"], 5e-9)
        self.assertAlmostEqual(sum(phases.values()), 100e-9)
        self.assertEqual(events, original)

    def test_baseline_can_have_measured_inline_time(self):
        phases = partition([counter()], 10, 90, (10, 90))
        self.assertAlmostEqual(phases["architecture"], 65e-9)
        self.assertAlmostEqual(phases["inline_power_accounting"], 15e-9)

    def test_missing_is_unknown_and_required_missing_fails(self):
        self.assertIsNone(power_accounting_counter([], (10, 90)))
        with self.assertRaisesRegex(ValueError, "rerun with power-accounting timing"):
            power_accounting_counter([], (10, 90), required=True)

    def test_bad_counter_or_missing_dram_coverage_fails(self):
        for change in (dict(value=-1), dict(value=81), dict(value=float("nan")), dict(calls=0),
                       dict(method_version=99), dict(window_start_ns=0),
                       dict(dram_callbacks_registered=False), dict(kind="span")):
            with self.subTest(change=change), self.assertRaises(ValueError):
                power_accounting_counter([counter() | change], (10, 90), required=True)
        with self.assertRaisesRegex(ValueError, "expected one"):
            power_accounting_counter([counter(), counter()], (10, 90))

    def test_counter_cannot_consume_hook_time_or_be_partially_clipped(self):
        events = [counter(70), dict(phase="exchange", start_ns=20, end_ns=60)]
        with self.assertRaisesRegex(ValueError, "exceeds exclusive architecture"):
            partition(events, 10, 90, (10, 90))
        with self.assertRaisesRegex(ValueError, "cannot clip"):
            partition([counter()], 20, 90, (10, 90))
