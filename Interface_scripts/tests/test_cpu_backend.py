"""CPU backend configuration and native SoftHier protocol regressions."""

import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from Interface_scripts.thermal_solver import configure, settings
from Interface_scripts.tests import test_power_trace_streaming as streaming
from experiments.thermal_replay.ice import IceClient

ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "3D-ICE_30_Oct/bin"
SERVER, CLIENT = [NATIVE / f"3D-ICE-{name}-cpu" for name in ("Server", "Client")]


class SolverConfigurationTests(unittest.TestCase):
    def test_default_preserves_legacy_stack(self):
        self.assertEqual(configure(streaming.STACK), streaming.STACK)

    def test_solver_override_preserves_physics_and_output(self):
        updated = configure(streaming.STACK, "cpu_amgcl_cg_amg", 4)
        self.assertEqual(settings(updated), dict(solver_type="cpu_amgcl_cg_amg",
                                                solver_threads=4, solver_tolerance=1e-6))
        for marker, index in (("solver:", 0), ("output:", 1)):
            self.assertEqual(updated.split(marker)[index], streaming.STACK.split(marker)[index])
        self.assertIn("transient step 1e-05, slot 0.0001", updated)
        self.assertIn("initial temperature 300", updated)
        direct = configure(updated, "cpu_direct_lu", 1)
        self.assertIsNone(settings(direct)["solver_tolerance"])
        self.assertNotIn("tolerance", direct)

    def test_invalid_controls(self):
        for kwargs in (dict(solver_type="gpu_direct_lu"), dict(solver_threads=0),
                       dict(solver_threads=1.5), dict(solver_threads=True),
                       dict(solver_tolerance=0), dict(solver_tolerance=float("nan")),
                       dict(solver_tolerance=1e-6)):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                configure(streaming.STACK, **kwargs)

    def test_new_backend_requires_explicit_cpu_selection(self):
        command = ["bash", str(ROOT / "Interface_scripts/co-simulation/dice_backend.sh")]
        # Backend selection is testable without the optional source checkout.
        with tempfile.TemporaryDirectory(prefix="cpu-backend-selection-") as temporary:
            directory = Path(temporary)
            (directory / "sources").mkdir()
            (directory / "sources/cpu_amgcl_solver.cpp").write_text("// backend marker\n")
            native = directory / "bin"
            native.mkdir()
            for name in ("Server", "Client"):
                binary = native / f"3D-ICE-{name}-cpu"
                binary.write_text("#!/bin/sh\nexit 0\n")
                binary.chmod(0o755)
            env = dict(os.environ, DICE_DIR=str(directory), DICE_BIN_DIR=str(native),
                       DICE_BUILD_TARGET="all", BUILD_3DICE="0")
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("DICE_BUILD_TARGET=cpu", result.stderr)
            result = subprocess.run(command, env=dict(env, DICE_BUILD_TARGET="cpu"),
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(native / "3D-ICE-Server-cpu"), result.stdout)


@unittest.skipUnless(SERVER.is_file() and CLIENT.is_file(), "requires new CPU binaries")
class CpuTransportTests(streaming.PowerTraceTransportTests):
    poll_seconds = .005

    def setUp(self):
        self.enterContext(patch.multiple(streaming, SERVER=SERVER, CLIENT=CLIENT, NATIVE=NATIVE))
        super().setUp()

    def test_client_leaves_server_feedback_unchanged(self):
        self.run_case("local")
        reference = (self.directory / "1/average.txt").read_bytes()
        self.run_case("socket")
        self.assertEqual((self.directory / "2/average.txt").read_bytes(), reference)

    def test_optional_client_output_uses_a_separate_directory(self):
        server_dir, client_dir = self.directory / "server", self.directory / "client"
        server_dir.mkdir()
        client_dir.mkdir()
        (server_dir / "ice.stk").write_text(streaming.STACK)
        (server_dir / "floorplan.flp").write_text(streaming.FLOORPLAN)
        power = client_dir / "power.txt"
        power.write_text(".1 .2\n.2 .1\n-1 -1\n")
        port = str(IceClient.select_port())
        log = server_dir / "server.log"
        with log.open("w") as stream:
            server = subprocess.Popen([str(SERVER), "ice.stk", port], cwd=server_dir,
                                      stdout=stream, stderr=subprocess.STDOUT)
            try:
                self.wait_log(log, "Waiting for client", [server])
                result = subprocess.run([str(CLIENT), "127.0.0.1", port, str(power),
                                         "--follow", "--until-minus-one", "--poll", ".005", "--client-output"],
                                        cwd=client_dir, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(server.wait(timeout=15), 0, log.read_text())
                server_rows = np.array(self.rows(server_dir / "average.txt"))
                client_rows = np.array(self.rows(client_dir / "average.txt"))
                np.testing.assert_allclose(client_rows[:, 0], [.0001, .0002], rtol=1e-7, atol=0)
                np.testing.assert_allclose(client_rows[:, 1:], server_rows[:, 1:], rtol=0, atol=.00100001)
            finally:
                if server.poll() is None:
                    server.kill()
                    server.wait()


@unittest.skipUnless(SERVER.is_file(), "requires new CPU server")
class CpuStateTests(unittest.TestCase):
    def test_full_state_geometry_zero_power_and_scoped_threads(self):
        for uniform in (False, True):
            for solver, threads in (("cpu_direct_lu", 1), ("cpu_amgcl_cg_amg", 4)):
                with self.subTest(uniform=uniform, solver=solver), tempfile.TemporaryDirectory() as tmp:
                    folder = Path(tmp)
                    text = streaming.STACK.replace("non-uniform true;", "non-uniform false;" if uniform else "non-uniform true;")
                    (folder / "ice.stk").write_text(configure(text, solver, threads))
                    (folder / "floorplan.flp").write_text(streaming.FLOORPLAN)
                    # An inherited one-thread limit must not silently cap explicit CG threads.
                    with patch.dict(os.environ, OMP_NUM_THREADS="1"), IceClient(SERVER, folder, solver_threads=threads) as ice:
                        self.assertEqual(ice.count, 2)
                        first = ice.state()
                        self.assertEqual(len(first), 200)
                        np.testing.assert_allclose(first, 26.85, atol=1e-4, rtol=0)
                        ice.advance([0., 0.])
                        np.testing.assert_allclose(ice.state(), first, atol=1e-4, rtol=0)
                        geometry = ice.exchange(9)
                        count, length, rows, columns = struct.unpack_from("=IIII", geometry)
                        self.assertEqual(count, 1)
                        self.assertEqual(rows * columns, 100)
                        self.assertEqual(geometry[16:16 + length], b"map.txt")
                        offset = 16 + ((length + 3) // 4) * 4
                        cells = np.frombuffer(geometry, dtype="=f4", offset=offset).reshape(-1, 4)
                        self.assertEqual(len(cells), 100)
                        self.assertAlmostEqual(float(np.sum(cells[:, 2] * cells[:, 3])), 1e6)
                        ice.advance([.1, .2])
                        self.assertGreater(ice.state().max(), first.max())
                        self.assertEqual(os.environ["OMP_NUM_THREADS"], "1")
                    self.assertIn(f"Used cores ({threads})", (folder / "server.log").read_text())


if __name__ == "__main__":
    unittest.main()
