"""Streaming, persistent-hook lifecycle, and failure-path regression checks."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from Interface_scripts.tests.test_interfaces import PowerHookTests, CO_SIMULATION_DIR, HOOK
from temperature_stream import TemperatureStream
from build_hook_client import build


class TemperatureStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "temperatures"
        self.reader = TemperatureStream(self.path, ["a", "b"])
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.reader.close)

    def append(self, text):
        with self.path.open("ab") as stream:
            stream.write(text.encode())

    def test_partial_headers_rows_and_repeated_times(self):
        self.assertIsNone(self.reader.read_row())
        self.append("% Time(s) a(K)")
        self.assertIsNone(self.reader.read_row())
        self.append(" b(K)\r\n\n0.000 30")
        self.assertIsNone(self.reader.read_row())
        before = self.reader.bytes_read
        for _ in range(10):
            self.assertIsNone(self.reader.read_row())
        self.assertEqual(self.reader.bytes_read, before)
        self.append("0 310\n0.000 320 330\n")
        self.assertEqual(self.reader.read_row(), (0., [300., 310.]))
        self.assertEqual(self.reader.read_row(), (0., [320., 330.]))
        self.assertIsNone(self.reader.read_row())
        self.assertEqual(self.reader.bytes_read, self.path.stat().st_size)

    def test_long_history_is_read_once_and_memory_is_bounded(self):
        self.append("% Time(s) a(K) b(K)\n")
        for i in range(10000):
            self.append("0.000 300 301\n")
            self.assertEqual(self.reader.read_row(), (0., [300., 301.]))
            self.assertIsNone(self.reader.read_row())
        self.assertEqual(self.reader.rows, 10000)
        self.assertEqual(self.reader.bytes_read, self.path.stat().st_size)
        self.assertLess(self.reader.peak_buffer_bytes, 65537)

    def test_rejects_replacement_and_truncation(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                self.reader.close()
                self.path.write_text("0 300 310\n")
                self.reader = TemperatureStream(self.path, ["a", "b"])
                self.reader.read_row()
                if replacement:
                    new = self.path.with_suffix(".new")
                    new.write_text("0 300 310\n")
                    new.replace(self.path)
                else:
                    self.path.write_text("")
                with self.assertRaisesRegex(ValueError, "replaced or truncated"):
                    self.reader.read_row()
                self.reader.close()

    def test_rejects_invalid_rows_and_headers(self):
        for text in ("0 nan 300\n", "0 300 0\n", "0 300\n", "0 bad 300\n",
                     "% Time(s) b(K) a(K)\n", "% Time(s) a(K) b\n"):
            with self.subTest(text=text):
                self.path.write_text(text)
                reader = TemperatureStream(self.path, ["a", "b"])
                try:
                    with self.assertRaises(ValueError):
                        reader.read_row()
                finally:
                    reader.close()


class PersistentHookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = build()

    def setUp(self):
        self.fixture = PowerHookTests()
        self.fixture.setUp()
        self.worker = None
        self.log = None
        self.socket = self.fixture.root / "worker.sock"
        self.ready = self.fixture.root / "ready.json"
        self.diag = self.fixture.root / "diagnostics.json"

    def tearDown(self):
        if self.worker is not None:
            if self.worker.poll() is None:
                self.worker.terminate()
            self.worker.wait(timeout=5)
        if self.log:
            self.log.close()
        self.fixture.tearDown()

    def start(self, *extra):
        f = self.fixture
        self.log = (f.root / "worker.log").open("w")
        self.worker = subprocess.Popen([sys.executable, str(CO_SIMULATION_DIR / "power_hook_worker.py"),
            "--socket", str(self.socket), "--ready", str(self.ready), "--diagnostics", str(self.diag),
            "--config", str(f.config_path), "--request", str(f.request_path), "--response", str(f.response_path),
            *extra], stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 5
        while not self.ready.exists():
            if self.worker.poll() is not None or time.monotonic() > deadline:
                self.fail((f.root / "worker.log").read_text())
            time.sleep(.005)

    def call(self, phase, start=0, end=0, success=True, equivalent_paths=False):
        f = self.fixture
        f.response_path.unlink(missing_ok=True)  # Same stale-response handling as GVSoC.
        f.request_path.write_text(json.dumps(dict(protocol=dict(name="gvsoc-power-hook", version=1),
            phase=phase, temperature_unit="celsius", window=dict(start_ps=start, end_ps=end),
            components=[] if phase == "init" else f.samples())))
        def path(p):
            return str(p.parent) + "/./" + p.name if equivalent_paths else str(p)
        result = subprocess.run([str(self.client), "--phase", phase, "--config", path(f.config_path),
            "--request", path(f.request_path), "--response", path(f.response_path)],
            env=dict(os.environ, POWER_HOOK_SOCKET=str(self.socket), POWER_HOOK_TIMEOUT_SECONDS="2"),
            capture_output=True, text=True, timeout=5)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr + (f.root / "worker.log").read_text())
            return json.loads(f.response_path.read_text())
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(f.response_path.exists())
        return result

    def test_persistent_lifecycle_matches_legacy_and_caches_configuration(self):
        f = self.fixture
        golden = [f.invoke("init", 0, 0, [])]
        for n, phase, start, end in ((1, "update", 0, 20), (2, "final", 20, 25)):
            thread = f.publish_temperature_after_power_row(n, f"0.000 {300+n} 305 320")
            golden.append(f.invoke(phase, start, end, f.samples()))
            thread.join()
        power = f.power_path.read_text()
        history = f.history_path.read_text()
        f.power_path.unlink(); f.history_path.unlink()
        f.temperature_path.write_text("% Time(s) chip__compute(K) chip__control(K) chip__memory(K)\n")
        self.start()
        actual = [self.call("init")]
        for n, phase, start, end in ((1, "update", 0, 20), (2, "final", 20, 25)):
            thread = f.publish_temperature_after_power_row(n, f"0.000 {300+n} 305 320")
            actual.append(self.call(phase, start, end)); thread.join()
        self.assertEqual(self.worker.wait(timeout=3), 0)
        self.assertEqual(actual, golden)
        self.assertEqual(f.power_path.read_text(), power)
        self.assertEqual(f.history_path.read_text(), history)
        d = json.loads(self.diag.read_text())
        self.assertEqual((d["configuration_loads"], d["exchanges"], d["requests"]), (1, 2, 3))
        self.assertEqual(d["readers"][0]["bytes_read"], f.temperature_path.stat().st_size)
        self.assertFalse(self.socket.exists())

    def test_duplicate_request_fails_without_appending_power(self):
        self.start(); self.call("init")
        self.call("init", success=False)
        self.assertNotEqual(self.worker.wait(timeout=3), 0)
        self.assertFalse(self.fixture.power_path.exists())

    def test_client_accepts_equivalent_file_paths(self):
        self.start()
        self.call("init", equivalent_paths=True)
        self.call("final", equivalent_paths=True)
        self.assertEqual(self.worker.wait(timeout=3), 0)

    def test_temperature_timeout_fails_without_retry(self):
        config = json.loads(self.fixture.config_path.read_text())
        config["timeout_seconds"] = .1
        self.fixture.config_path.write_text(json.dumps(config))
        self.start(); self.call("init")
        result = self.call("update", 0, 20, success=False)
        self.assertIn("timed out", result.stderr)
        self.assertNotEqual(self.worker.wait(timeout=3), 0)
        self.assertEqual(len(self.fixture.power_path.read_text().splitlines()), 1)

    def test_cancellation_removes_socket_and_records_failure(self):
        self.start(); self.call("init")
        self.worker.terminate(); self.worker.wait(timeout=3)
        self.assertFalse(self.socket.exists())
        self.assertFalse(self.ready.exists())
        self.assertEqual(json.loads(self.diag.read_text())["status"], "failed")

    def test_cancellation_during_exchange_unblocks_client_without_retry(self):
        self.start(); self.call("init")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.call, "update", 0, 20, False)
            deadline = time.monotonic() + 3
            while not self.fixture.power_path.exists():
                if time.monotonic() > deadline:
                    self.fail("worker did not publish the power window")
                time.sleep(.005)
            self.worker.terminate()
            self.assertIn("signal", future.result(timeout=5).stderr)
        self.assertEqual(len(self.fixture.power_path.read_text().splitlines()), 1)

    def test_socket_collision_does_not_remove_another_workers_socket(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as other:
            other.bind(str(self.socket))
            with self.assertRaises(AssertionError):
                self.start()
            self.assertTrue(self.socket.exists())

    def test_worker_detects_thermal_process_failure(self):
        thermal = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.start("--watch-pid", str(thermal.pid)); self.call("init")
            thermal.terminate(); thermal.wait(timeout=3)
            self.assertNotEqual(self.worker.wait(timeout=3), 0)
            self.assertIn("exited", json.loads(self.diag.read_text())["error"])
        finally:
            if thermal.poll() is None:
                thermal.kill(); thermal.wait()

    def test_zero_duration_final_does_not_advance_thermal_state(self):
        self.start(); self.call("init"); self.call("final")
        self.assertEqual(self.worker.wait(timeout=3), 0)
        self.assertEqual(json.loads(self.diag.read_text())["exchanges"], 0)
        self.assertEqual(self.fixture.power_path.read_text().split(), ["-1", "-1", "-1"])


class MultiDieStreamingTests(unittest.TestCase):
    def test_both_stack_orders_wait_for_complete_matching_rows(self):
        from Interface_scripts.tests import test_memory_on_logic as fixtures
        for fixture_type in (fixtures.MultiDieHookTests, fixtures.LogicOnDramHookTests):
            with self.subTest(order=fixture_type.stack_order):
                case = fixture_type()
                session = None
                try:
                    case.setUp()
                    session = fixtures.HOOK.HookSession(case.config_path)
                    original = fixtures.HOOK.run
                    with patch.object(fixtures.HOOK, "run", side_effect=lambda args: original(args, session=session)):
                        case.test_init_voltage_and_both_die_rows_required_for_each_slot()
                    self.assertEqual([r.rows for r in session.readers], [2, 2])
                finally:
                    if session:
                        session.close()
                    case.doCleanups()


class NativePollTests(unittest.TestCase):
    def test_fractional_client_polling_preserves_partial_rows_and_final_maps(self):
        from Interface_scripts.tests import test_memory_on_logic as fixtures
        root = CO_SIMULATION_DIR.parents[1]
        server, client = [root / "3D-ICE/bin" / name for name in ("3D-ICE-Server", "3D-ICE-Client")]
        if not server.exists() or not client.exists():
            self.skipTest("requires built native binaries")
        case = fixtures.MultiDieHookTests()
        processes, logs = [], []
        try:
            case.setUp()
            folder = case.folder
            stack = folder / "ice.stk"
            text = stack.read_text()
            for die in case.manifest["dies"]:
                text = text.replace(f'"{die["map_output"]}", slot)', f'"{die["map_output"]}", final)')
            stack.write_text(text)
            power = folder / "native_power.txt"
            power.write_text("0.0")
            width = len(case.manifest["power_order"])
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = str(reservation.getsockname()[1])
            def launch(command, name):
                log = (folder / name).open("w"); logs.append(log)
                process = subprocess.Popen(command, cwd=folder, stdout=log, stderr=subprocess.STDOUT,
                    env=dict(os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1"))
                processes.append(process)
                return process
            def wait_log(name, expected):
                deadline = time.monotonic() + 20
                while expected not in (folder / name).read_text():
                    self.assertTrue(all(p.poll() is None for p in processes), (folder / name).read_text())
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.005)
            launch([str(server), str(stack), port], "native_server.log")
            wait_log("native_server.log", "Waiting for client")
            launch([str(client), "127.0.0.1", port, str(power), "--follow", "--until-minus-one", "--poll", ".005"],
                   "native_client.log")
            wait_log("native_client.log", "Waiting for power trace values")
            with power.open("a") as stream:
                stream.write("1 " + " ".join(["0.01"] * (width - 1)))
            time.sleep(.04)
            self.assertNotIn("Slot 0 simulated", (folder / "native_client.log").read_text())
            with power.open("a") as stream:
                stream.write("\n" + " ".join(["-1"] * width) + "\n")
            for process in reversed(processes):
                self.assertEqual(process.wait(timeout=20), 0)
            for die in case.manifest["dies"]:
                rows = [line for line in (folder / die["map_output"]).read_text().splitlines()
                        if line.strip() and not line.startswith("%")]
                self.assertTrue(rows, "final thermal map was not written")
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.kill()
                process.wait()
            for log in logs:
                log.close()
            case.doCleanups()

    def test_invalid_native_poll_intervals_are_rejected(self):
        root = CO_SIMULATION_DIR.parents[1]
        for name in ("3D-ICE-Server", "3D-ICE-Client"):
            binary = root / "3D-ICE/bin" / name
            if not binary.exists():
                self.skipTest("requires built native binaries")
            for value in ("0", "-1", "nan", "inf", "1oops"):
                args = ["missing.stk", "--power-trace", "/dev/null"] if name.endswith("Server") else [
                    "127.0.0.1", "12345", "/dev/null", "--until-minus-one"]
                result = subprocess.run([str(binary), *args, "--poll", value], capture_output=True, text=True, timeout=3)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--poll requires", result.stderr)


if __name__ == "__main__":
    unittest.main()
