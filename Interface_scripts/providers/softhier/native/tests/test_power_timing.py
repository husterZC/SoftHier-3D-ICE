"""Exercise real C++ counters across a separately loaded DRAM-like library."""
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import tempfile
import unittest

NATIVE = Path(__file__).resolve().parents[1]
DRAM = r'''
#define CLO_DRAM_TIMING_IMPLEMENTATION
#include "dram_power_timing.hpp"
extern "C" void dram_work(void (*work)()) { CloDramPowerScope timer; work(); }
'''
ENGINE = r'''
#include <time.h>
#include <dlfcn.h>
#include <thread>
#include <cassert>
#include <cstring>
static int64_t ticks = 0;
extern "C" int clock_gettime(clockid_t, timespec *ts) noexcept
{ ts->tv_sec = ticks / 1000000000; ts->tv_nsec = ticks % 1000000000; return 0; }
#include "power_accounting_timing_impl.hpp"
int main(int argc, char **argv) {
    assert(argc == 3);
    void *library = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL); assert(library);
    using Register = int (*)(int (*)(), void (*)(int));
    auto reg = (Register)dlsym(library, "softhier_dram_set_power_timing_callbacks");
    auto dram = (void (*)(void (*)()))dlsym(library, "dram_work");
    assert(reg && dram && reg(clo_power_timing_enter, clo_power_timing_leave) == 1);
    clo_power_timing_dram_registered();
    { CloPowerScope outside; ticks = 100; }
    clo_power_window_begin(ticks, 1000, 0);
    if (!strcmp(argv[2], "thread")) {
        std::thread worker([] { CloPowerScope wrong_thread; }); worker.join();
    }
    ticks = 120;
    {
        CloPowerScope outer;
        ticks = 130; { CloPowerScope nested; ticks = 150; }
        ticks = 160;
        {
            CloPowerPause pause;
            ticks = 180; { CloPowerPause nested_pause; CloPowerScope excluded; ticks = 200; }
            ticks = 220;
        }
        ticks = 250;
        if (!strcmp(argv[2], "unbalanced")) clo_power_window_end(ticks, 2000, 0);
    }
    ticks = 270;
    dram([] { ticks = 280; { CloPowerScope nested_logic; ticks = 300; } ticks = 310; });
    ticks = 400;
    clo_power_window_end(ticks, 2000, 0);
    ticks = 500; clo_power_window_begin(ticks, 3000, 0);
    ticks = 600; clo_power_window_end(ticks, 4000, 0);
}
'''


@unittest.skipUnless(shutil.which("g++"), "requires C++ compiler")
class NativePowerTimingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.folder = Path(cls.temp.name)
        compiler = shutil.which("g++")
        for name, source in (("engine", ENGINE), ("dram", DRAM)):
            path = cls.folder / (name + ".cpp")
            path.write_text(source)
            command = [compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-I", str(NATIVE), str(path)]
            command += ["-shared", "-fPIC", "-o", str(cls.folder / "dram.so")] if name == "dram" else [
                "-pthread", "-ldl", "-o", str(cls.folder / "engine")]
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_counter(self, enabled, mode="normal", external=False):
        directory = self.folder / self.id().rsplit(".", 1)[-1]
        directory.mkdir()
        env = dict(os.environ, CLO_TIMING_DIR=str(directory),
                   CLO_POWER_ACCOUNTING_TIMING=str(int(enabled)),
                   CLO_POWER_TIMING_EXTERNAL_WINDOW=str(int(external)))
        result = subprocess.run([str(self.folder / "engine"), str(self.folder / "dram.so"), mode],
                                env=env, cwd=directory, capture_output=True, text=True,
                                preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)))
        counters = [e for path in directory.glob("*.jsonl")
                    for e in map(json.loads, path.read_text().splitlines())
                    if e["phase"] == "inline_power_accounting"]
        return result, counters

    def test_nested_pause_and_cross_library_callbacks_count_once(self):
        result, counters = self.run_counter(True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([e["value"] for e in counters], [110, 0])
        self.assertEqual([e["calls"] for e in counters], [2, 0])
        self.assertEqual(counters[0]["window_start_ns"], 100)
        self.assertEqual(counters[0]["window_end_ns"], 400)
        self.assertEqual(counters[0]["start_ns"], counters[0]["end_ns"])
        self.assertTrue(counters[0]["dram_callbacks_registered"])

    def test_disabled_has_no_counter_records(self):
        result, counters = self.run_counter(False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(counters, [])

    def test_external_windows_ignore_workload_markers(self):
        result, counters = self.run_counter(True, external=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(counters, [])

    def test_overlapping_threads_are_rejected(self):
        result, _ = self.run_counter(True, "thread")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("concurrent accounting threads", result.stderr)

    def test_unbalanced_window_is_rejected(self):
        result, _ = self.run_counter(True, "unbalanced")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unbalanced measurement window", result.stderr)
