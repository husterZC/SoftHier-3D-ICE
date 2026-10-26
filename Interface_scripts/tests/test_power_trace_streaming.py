"""Regressions for partially appended 3D-ICE power slots; all artifacts are temporary."""

import ctypes
import math
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "3D-ICE/bin"
SERVER = NATIVE / "3D-ICE-Server"
CLIENT = NATIVE / "3D-ICE-Client"


class ReaderState(ctypes.Structure):
    _fields_ = [
        ("stream", ctypes.c_void_p), ("buffer", ctypes.c_void_p),
        ("capacity", ctypes.c_size_t), ("begin", ctypes.c_size_t),
        ("end", ctypes.c_size_t), ("scan", ctypes.c_size_t),
        ("follow", ctypes.c_int), ("error", ctypes.c_char * 192),
    ]


@unittest.skipUnless(shutil.which("cc"), "requires a C compiler")
class PowerTraceReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="power-reader-tests-")
        cls.addClassCleanup(temporary.cleanup)
        library = Path(temporary.name) / "reader.so"
        subprocess.run([
            "cc", "-std=c99", "-O3", "-Wall", "-Wextra", "-Werror",
            "-fPIC", "-shared", str(NATIVE / "power_trace_reader.c"),
            "-o", str(library),
        ], check=True, capture_output=True, text=True)
        cls.lib = ctypes.CDLL(str(library))
        cls.lib.power_trace_reader_init.argtypes = [ctypes.POINTER(ReaderState), ctypes.c_void_p, ctypes.c_int]
        cls.lib.power_trace_reader_init.restype = None
        cls.lib.power_trace_reader_destroy.argtypes = [ctypes.POINTER(ReaderState)]
        cls.lib.power_trace_reader_destroy.restype = None
        cls.lib.power_trace_reader_next.argtypes = [
            ctypes.POINTER(ReaderState), ctypes.c_size_t, ctypes.c_int,
            ctypes.POINTER(ctypes.c_float),
        ]
        cls.lib.power_trace_reader_next.restype = ctypes.c_int
        cls.libc = ctypes.CDLL(None)
        cls.libc.fopen.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        cls.libc.fopen.restype = ctypes.c_void_p
        cls.libc.fclose.argtypes = [ctypes.c_void_p]
        cls.libc.fclose.restype = ctypes.c_int
        cls.libc.ftell.argtypes = [ctypes.c_void_p]
        cls.libc.ftell.restype = ctypes.c_long

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="power-reader-case-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.counter = 0

    def reader(self, data=b"", width=2, follow=True):
        self.counter += 1
        path = self.directory / str(self.counter)
        path.write_bytes(data)
        stream = self.libc.fopen(os.fsencode(path), b"rb")
        self.assertTrue(stream)
        state = ReaderState()
        self.lib.power_trace_reader_init(ctypes.byref(state), stream, follow)
        powers = (ctypes.c_float * width)(*([-999.0] * width))

        def cleanup():
            self.lib.power_trace_reader_destroy(ctypes.byref(state))
            self.libc.fclose(stream)
        self.addCleanup(cleanup)

        def append(data):
            with path.open("ab") as out:
                out.write(data)

        def read(termination=True):
            return self.lib.power_trace_reader_next(ctypes.byref(state), width, termination, powers)

        return state, powers, append, read

    def test_split_decimal_and_exponent_wait_for_newline(self):
        state, powers, append, read = self.reader()
        self.assertEqual(read(), 0)
        for fragment in [b"0.0", b"0114336998902929 2.5e", b"-", b"3"]:
            append(fragment)
            self.assertEqual(read(), 0)
            self.assertEqual(list(powers), [-999.0, -999.0])
        position = self.libc.ftell(state.stream)
        storage = (state.buffer, state.capacity, state.begin, state.end, state.scan)
        for _ in range(20):
            self.assertEqual(read(), 0)
            self.assertEqual(self.libc.ftell(state.stream), position)
            self.assertEqual((state.buffer, state.capacity, state.begin, state.end, state.scan), storage)
        append(b"\n")
        self.assertEqual(read(), 1)
        self.assertAlmostEqual(powers[0], 0.00114336998902929, places=9)
        self.assertAlmostEqual(powers[1], 0.0025, places=9)
        append(b"0.2 0.3\n")
        self.assertEqual(read(), 1)
        self.assertAlmostEqual(powers[0], 0.2, places=7)
        self.assertAlmostEqual(powers[1], 0.3, places=7)

    def test_crlf_blank_lines_and_multiple_buffered_slots(self):
        _, powers, _, read = self.reader(b" \r\n1\t2\r\n 3 4 \n")
        self.assertEqual(read(), 1)
        self.assertEqual(list(powers), [1, 2])
        self.assertEqual(read(), 1)
        self.assertEqual(list(powers), [3, 4])
        self.assertEqual(read(), 0)

    def test_wide_fragmented_rows_reuse_bounded_storage(self):
        width = 1024
        row = b" ".join([b"0.00114336998902929"] * width) + b"\n"
        state, powers, append, read = self.reader(width=width)
        for offset in range(0, len(row), 127):
            append(row[offset:offset + 127])
            self.assertEqual(read(), 1 if offset + 127 >= len(row) else 0)
        capacity = state.capacity
        self.assertLessEqual(capacity, len(row) * 2 + 8193)
        append(row * 200)
        for _ in range(200):
            self.assertEqual(read(), 1)
            self.assertEqual(state.capacity, capacity)
            self.assertAlmostEqual(powers[-1], 0.00114336998902929, places=9)
        self.assertEqual(read(), 0)

    def test_rejects_malformed_rows_before_returning_a_slot(self):
        for row in [b"1\n", b"1 2 3\n", b"1x 2\n", b"1e 2\n", b"nan 2\n",
                    b"inf 2\n", b"1e999 2\n", b"-0.5 2\n", b"-1 2\n", b"1\x00 2\n"]:
            with self.subTest(row=row):
                state, _, _, read = self.reader(row)
                self.assertEqual(read(), -1)
                self.assertTrue(state.error)

    def test_termination_requires_a_complete_row_and_enabled_sentinel(self):
        _, powers, append, read = self.reader(b"-1 -1")
        self.assertEqual(read(), 0)
        append(b"\n")
        self.assertEqual(read(), 1)
        self.assertEqual(list(powers), [-1, -1])
        _, _, _, disabled = self.reader(b"-1 -1\n")
        self.assertEqual(disabled(termination=False), -1)

    def test_static_whitespace_and_final_value_without_newline(self):
        _, powers, _, read = self.reader(b" 1\n2 3\t4", follow=False)
        self.assertEqual(read(), 1)
        self.assertEqual(list(powers), [1, 2])
        self.assertEqual(read(), 1)
        self.assertEqual(list(powers), [3, 4])
        self.assertEqual(read(), 2)

    def test_static_truncated_and_invalid_final_slots(self):
        for data in [b"1", b"1 2x", b"1 nan"]:
            with self.subTest(data=data):
                _, _, _, read = self.reader(data, follow=False)
                self.assertEqual(read(), -1)


STACK = """material SILICON :
 thermal conductivity 1.30e-4 ;
 volumetric heat capacity 1.628e-12 ;
material BEOL :
 thermal conductivity 2.25e-6 ;
 volumetric heat capacity 2.175e-12 ;
top heat sink :
 heat transfer coefficient 1e-07 ;
 temperature 300 ;
dimensions :
 chip length 1000, width 1000 ;
 cell length 100, width 100 ;
 non-uniform true;
layer PCB :
 height 50 ;
 material BEOL ;
die IC :
 source 50 SILICON ;
stack:
 die TOP_DIE IC floorplan "floorplan.flp" ;
 layer CONN_TO_PCB PCB ;
solver:
 transient step 1e-05, slot 0.0001 ;
 initial temperature 300 ;
 numofcores 1 ;
output:
 Tflp(TOP_DIE, "average.txt", average, slot);
 Tmap(TOP_DIE, "map.txt", slot);
"""
FLOORPLAN = """left :
 position 0, 0;
 dimension 500, 1000;
 discretization 5, 10;
right :
 position 500, 0;
 dimension 500, 1000;
 discretization 5, 10;
"""


@unittest.skipUnless(SERVER.is_file() and CLIENT.is_file(), "requires built 3D-ICE binaries")
class PowerTraceTransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="power-transport-tests-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.counter = 0

    @staticmethod
    def rows(path):
        if not path.exists():
            return []
        return [list(map(float, line.split())) for line in path.read_text().splitlines()
                if line.strip() and not line.startswith("%")]

    def wait_log(self, path, marker, processes):
        deadline = time.monotonic() + 15
        while marker not in path.read_text():
            self.assertTrue(all(p.poll() is None for p in processes), path.read_text())
            self.assertLess(time.monotonic(), deadline, path.read_text())
            time.sleep(0.01)

    def run_case(self, mode, fragmented=False, invalid=False, random=False):
        self.counter += 1
        directory = self.directory / str(self.counter)
        directory.mkdir()
        (directory / "ice.stk").write_text(STACK)
        (directory / "floorplan.flp").write_text(FLOORPLAN)
        trace = directory / "power.txt"
        complete = b"0.00114336998902929 0.02\n0.5 0.003\n-1 -1\n"
        trace.write_bytes(b"0.0" if fragmented else b"1 2 3\n" if invalid else complete)
        processes = []
        logs = []

        def launch(binary, args, name):
            log = (directory / name).open("w")
            logs.append(log)
            process = subprocess.Popen([str(binary), *args], cwd=directory,
                                       stdout=log, stderr=subprocess.STDOUT)
            processes.append(process)
            return process

        try:
            if mode == "local":
                args = ["ice.stk", "--power-trace", "power.txt", "--until-minus-one"]
                if fragmented or invalid:
                    args.append("--follow")
                reader = launch(SERVER, args, "server.log")
                reader_log = directory / "server.log"
            else:
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = str(sock.getsockname()[1])
                launch(SERVER, ["ice.stk", port], "server.log")
                self.wait_log(directory / "server.log", "Waiting for client", processes)
                args = ["2", "127.0.0.1", port] if random else [
                    "127.0.0.1", port, "power.txt", "--follow", "--until-minus-one"]
                reader = launch(CLIENT, args, "client.log")
                reader_log = directory / "client.log"

            if fragmented:
                self.wait_log(reader_log, "Waiting for power trace values for slot 0", processes)
                self.assertEqual(self.rows(directory / "map.txt"), [])
                with trace.open("ab") as stream:
                    stream.write(b"0114336998902929 0.02")
                # Allow a full polling interval: even a complete final number
                # must not commit a row until its newline has arrived.
                time.sleep(1.2)
                self.assertEqual(self.rows(directory / "map.txt"), [])
                with trace.open("ab") as stream:
                    stream.write(b"\n0.5 0.003\n-1 -1\n")

            status = reader.wait(timeout=15)
            if invalid:
                self.assertNotEqual(status, 0)
                self.assertIn("more than 2 values", reader_log.read_text())
                self.assertEqual(self.rows(directory / "map.txt"), [])
            else:
                self.assertEqual(status, 0, reader_log.read_text())
                for process in processes:
                    self.assertEqual(process.wait(timeout=15), 0, (directory / "server.log").read_text())
                rows = self.rows(directory / "map.txt")
                self.assertEqual(len(rows), 2)
                self.assertTrue(all(math.isfinite(v) and 290 < v < 400 for row in rows for v in row))
                return rows
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for log in logs:
                log.close()

    def test_fragmented_input_matches_static_replay_in_both_transports(self):
        baseline = self.run_case("local")
        for mode in ["local", "socket"]:
            with self.subTest(mode=mode):
                actual = self.run_case(mode, fragmented=True)
                for expected_row, actual_row in zip(baseline, actual):
                    self.assertEqual(len(actual_row), len(expected_row))
                    for expected, actual in zip(expected_row, actual_row):
                        self.assertAlmostEqual(actual, expected, places=3)

    def test_invalid_rows_never_reach_thermal_solver(self):
        for mode in ["local", "socket"]:
            with self.subTest(mode=mode):
                self.run_case(mode, invalid=True)

    def test_client_random_power_mode(self):
        self.run_case("socket", random=True)


if __name__ == "__main__":
    unittest.main()
