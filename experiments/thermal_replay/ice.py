"""Persistent local 3D-ICE socket client (native-endian, four-byte words)."""

import errno
import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import time

import numpy as np

from .activity import load_contract
from .layout import Layout
from thermal_manifest import copy_inputs
from Interface_scripts.thermal_solver import configure as configure_solver, environment as solver_environment
EXIT, OUTPUT, COUNT, POWERS, SLOT, STATE = 0, 2, 4, 5, 6, 10


def prepare_stack(recording, output, dense_maps=False, factorization_threads=1, thermal_step=None,
                  solver_type=None, solver_tolerance=None):
    """Prepare replay inputs, defaulting to one thermal step per power slot."""
    recording, output = Path(recording), Path(output)
    # prepare_stack is also used directly by legacy callers with only stack/FLP.
    contract = recording / "system_config.json"
    multidie = contract.is_file() and load_contract(contract)["contract"]["version"] == 2
    if multidie:
        layout = Layout(recording, load_contract(contract))
        copy_inputs(recording / "thermal_manifest.json", output / "thermal_manifest.json",
                    recording / "ice.stk", output / "ice.stk")
        source = (output / "ice.stk").read_text()
        dies = layout.dies
    else:
        source = (recording / "ice.stk").read_text()
        matches = re.findall(r'\bdie\s+(\w+)\s+\w+\s+floorplan\s+"', source)
        if len(matches) != 1:
            raise ValueError("cannot identify a single powered die without a multi-die manifest")
        source = re.sub(r'\bfloorplan\s+"[^"]+"', 'floorplan "floorplan.flp"', source)
        dies = [{"id": matches[0], "temperature_output": "floorplan_temperatures.txt", "map_output": "temperature_map.txt"}]
        (output / "floorplan.flp").write_bytes((recording / "floorplan.flp").read_bytes())
    # SuperLU_MT's parallel factorization intermittently produced incorrect
    # zero-power solutions on small grids in validation. Use its serial path
    # by default; this changes execution concurrency, not thermal parameters.
    source, thread_fields = re.subn(r"\bnumofcores\s+\d+\s*;",
                                    f"numofcores {factorization_threads} ;", source)
    if thread_fields != 1:
        raise ValueError("missing or ambiguous factorization thread setting")
    source = configure_solver(source, solver_type, factorization_threads, solver_tolerance)
    solver = re.search(r"transient\s+step\s+([\deE.+-]+)\s*,\s*slot\s+([\deE.+-]+)", source)
    if not solver:
        raise ValueError("replay requires a transient stack")
    slot_ps = round(float(solver[2]) * 1e12)
    if thermal_step is not None and (not np.isfinite(thermal_step) or thermal_step <= 0):
        raise ValueError("thermal step must be a positive finite number of seconds")
    step_ps = slot_ps if thermal_step is None else round(thermal_step * 1e12)
    if step_ps <= 0 or slot_ps < step_ps or slot_ps % step_ps:
        raise ValueError("thermal slot must be an integer multiple of the integration step")
    source = source[:solver.start(1)] + f"{step_ps / 1e12:.12g}" + source[solver.end(1):]
    outputs = "".join(f' Tflp({die["id"]}, "{die["temperature_output"]}", average, slot);\n' +
                      (f' Tmap({die["id"]}, "{die["map_output"]}", slot);\n' if dense_maps else "") for die in dies)
    source, count = re.subn(r"\boutput\s*:.*\Z", "output:\n" + outputs,
                           source, flags=re.S)
    if count != 1:
        raise ValueError("missing stack output section")
    (output / "ice.stk").write_text(source)
    return step_ps, slot_ps


class IceClient:
    def __init__(self, server, directory, timeout=180., solver_threads=None):
        self.server = str(Path(server).resolve())
        self.directory = Path(directory)
        self.timeout = timeout
        self.solver_threads = solver_threads
        self.process = self.sock = self.log = None
        self.startup_failures = []

    @staticmethod
    def select_port():
        with socket.socket() as reservation:
            # Match the native server's wildcard bind, not just loopback.
            reservation.bind(("0.0.0.0", 0))
            return reservation.getsockname()[1]

    @staticmethod
    def connect(port):
        candidate = socket.socket()
        try:
            candidate.settimeout(.2)
            candidate.bind(("127.0.0.1", 0))
            # Reject the server port BEFORE connecting. Detecting a self-
            # connection afterwards leaves TIME_WAIT and can prevent the
            # native server from binding after its lengthy factorization.
            if candidate.getsockname()[1] == port:
                candidate.close()
                return None
            candidate.connect(("127.0.0.1", port))
            return candidate
        except BaseException:
            candidate.close()
            raise

    def start_server(self, environment, deadline):
        self.port = self.select_port()
        self.log = (self.directory / "server.log").open("w")
        self.process = subprocess.Popen([self.server, "ice.stk", str(self.port)],
                                        cwd=self.directory, env=environment, stdout=self.log,
                                        stderr=subprocess.STDOUT, start_new_session=True)
        ready = False
        while self.sock is None:
            if self.process.poll() is not None:
                log = self.directory / "server.log"
                message = log.read_text()
                if "ERROR :: server bind: Address already in use" in message:
                    raise OSError(errno.EADDRINUSE, f"3D-ICE port {self.port} is occupied; see {log}")
                raise RuntimeError(f"3D-ICE exited during startup; see {log}")
            # This marker is flushed by 3D-ICE only after bind/listen succeeds.
            # Do not connect to a different service that claimed the port
            # while our own server was still preparing its thermal matrix.
            ready = ready or "Waiting for client ..." in (self.directory / "server.log").read_text()
            if ready:
                try:
                    self.sock = self.connect(self.port)
                except (ConnectionRefusedError, TimeoutError):
                    pass
            if self.sock is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError("3D-ICE startup timed out")
                time.sleep(.02)

    def archive_startup_failure(self, attempt, started, error):
        pid, port = self.process.pid, self.port
        self.close()
        log = self.directory / f"server_startup_failed_{attempt:02d}.log"
        (self.directory / "server.log").rename(log)
        failure = dict(attempt=attempt, pid=pid, port=port, error=str(error), log=str(log),
                       host_seconds=time.monotonic() - started)
        timing_dir = os.environ.get("CLO_TIMING_DIR")
        if timing_dir:
            timing = Path(timing_dir) / f"{pid}.jsonl"
            if timing.is_file():
                # Preserve failed setup evidence outside the successful
                # session's top-level step/factorization timing stream.
                archive = Path(timing_dir) / "startup_failed"
                archive.mkdir(exist_ok=True)
                saved = archive / f"{pid}_{attempt:02d}.jsonl"
                timing.rename(saved)
                failure["timing"] = str(saved)
        self.startup_failures.append(failure)

    def __enter__(self):
        try:
            environment = (solver_environment(self.solver_threads) if self.solver_threads is not None
                           else os.environ.copy())
            environment.setdefault("OPENBLAS_NUM_THREADS", "1")
            deadline = time.monotonic() + self.timeout
            for attempt in range(1, 4):
                started = time.monotonic()
                try:
                    self.start_server(environment, deadline)
                    break
                except OSError as error:
                    if error.errno != errno.EADDRINUSE or attempt == 3 or time.monotonic() >= deadline:
                        raise
                    self.archive_startup_failure(attempt, started, error)
            self.sock.settimeout(self.timeout)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.count = self.word(self.exchange(COUNT))
            return self
        except BaseException:
            self.close()
            raise

    def receive(self, length):
        chunks = bytearray()
        while len(chunks) < length:
            chunk = self.sock.recv(length - len(chunks))
            if not chunk:
                raise ConnectionError("3D-ICE closed the connection")
            chunks.extend(chunk)
        return bytes(chunks)

    def exchange(self, kind, payload=b""):
        self.sock.sendall(struct.pack("=II", 2 + len(payload) // 4, kind) + payload)
        if kind == EXIT:
            return b""
        size, actual = struct.unpack("=II", self.receive(8))
        if actual != kind or not 2 <= size <= 100_000_000:
            raise RuntimeError("invalid 3D-ICE reply header")
        return self.receive(4 * (size - 2))

    @staticmethod
    def word(payload):
        if len(payload) != 4:
            raise RuntimeError("unexpected 3D-ICE status reply")
        return struct.unpack("=I", payload)[0]

    def advance(self, powers):
        powers = np.asarray(powers, dtype="=f4")
        if powers.shape != (self.count,) or not np.isfinite(powers).all() or (powers < 0).any():
            raise ValueError("power vector differs from server floorplan or contains invalid values")
        if self.word(self.exchange(POWERS, struct.pack("=I", self.count) + powers.tobytes())) != 0:
            raise RuntimeError("3D-ICE rejected power slot")
        if self.word(self.exchange(SLOT)) != 4:
            raise RuntimeError("3D-ICE failed to advance a thermal slot")

    def temperatures(self, expected_entries):
        payload = self.exchange(OUTPUT, struct.pack("=III", 2, 2, 1))  # slot/Tflp/average
        if len(payload) < 8:
            raise RuntimeError("missing Tflp inspection point")
        dies = expected_entries if expected_entries and isinstance(expected_entries[0], dict) else [
            {"temperature_output": "floorplan_temperatures.txt", "elements": expected_entries}]
        _, count = struct.unpack_from("=fI", payload)
        if count != len(dies):
            raise RuntimeError("unexpected Tflp output count")
        expected = {d["temperature_output"]: d["elements"] for d in dies}
        found, offset = {}, 8
        for _ in range(count):
            if offset + 12 > len(payload):
                raise RuntimeError("truncated Tflp inspection point")
            filename_len, header_len, width = struct.unpack_from("=III", payload, offset)
            offset += 12
            name_end = offset + ((filename_len + 3) // 4) * 4
            header_end = name_end + ((header_len + 3) // 4) * 4
            end = header_end + width * 4
            if end > len(payload):
                raise RuntimeError("truncated Tflp payload")
            filename = payload[offset:offset + filename_len].decode()
            header = payload[name_end:name_end + header_len].decode()
            if filename not in expected or filename in found:
                raise RuntimeError("unknown or duplicate Tflp output filename")
            if width != len(expected[filename]) or re.findall(r"([^\s]+)\(K\)", header) != expected[filename]:
                raise RuntimeError("Tflp order differs from the recording floorplan")
            found[filename] = np.frombuffer(payload, dtype="=f4", count=width, offset=header_end).astype(float)
            offset = end
        if offset != len(payload):
            raise RuntimeError("malformed Tflp payload")
        values = np.concatenate([found[d["temperature_output"]] for d in dies])
        if not np.isfinite(values).all() or (values <= 0).any():
            raise RuntimeError("invalid thermal solution")
        return values - 273.15

    def state(self):
        try:
            payload = self.exchange(STATE)
        except TimeoutError as exc:
            raise RuntimeError("thermal state command unavailable; rebuild 3D-ICE") from exc
        if len(payload) < 8:
            raise RuntimeError("missing full thermal state")
        count = struct.unpack_from("=I", payload)[0]
        if len(payload) != 4 + 4 * count:
            raise RuntimeError("malformed thermal state")
        values = np.frombuffer(payload, dtype="=f4", offset=4).astype(float)
        if not np.isfinite(values).all() or (values <= 0).any():
            raise RuntimeError("invalid full thermal state")
        return values - 273.15

    def close(self):
        if self.sock is not None:
            try:
                self.exchange(EXIT)
            except OSError:
                pass
            self.sock.close()
            self.sock = None
        if self.process is not None:
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait()
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None

    def __exit__(self, *_):
        self.close()
