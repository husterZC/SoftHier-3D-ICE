"""Persistent local 3D-ICE socket client (native-endian, four-byte words)."""

import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import time

import numpy as np

EXIT, OUTPUT, COUNT, POWERS, SLOT, STATE = 0, 2, 4, 5, 6, 10


def prepare_stack(recording, output, dense_maps=False, factorization_threads=1, thermal_step=None):
    """Prepare replay inputs, defaulting to one thermal step per power slot."""
    source = (Path(recording) / "ice.stk").read_text()
    floorplans = re.findall(r'\bfloorplan\s+"[^"]+"', source)
    if len(floorplans) != 1:
        raise ValueError("replay currently requires a single powered die/floorplan")
    match = re.search(r'\bdie\s+(\w+)\s+\w+\s+floorplan\s+"', source)
    if not match:
        raise ValueError("cannot identify powered die in archived stack")
    die = match[1]
    source = re.sub(r'\bfloorplan\s+"[^"]+"', 'floorplan "floorplan.flp"', source)
    # SuperLU_MT's parallel factorization intermittently produced incorrect
    # zero-power solutions on small grids in validation. Use its serial path
    # by default; this changes execution concurrency, not thermal parameters.
    source, thread_fields = re.subn(r"\bnumofcores\s+\d+\s*;",
                                    f"numofcores {factorization_threads} ;", source)
    if thread_fields != 1:
        raise ValueError("missing or ambiguous factorization thread setting")
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
    source, count = re.subn(r"\boutput\s*:.*\Z", "output:\n" +
                           f' Tflp({die}, "floorplan_temperatures.txt", average, slot);\n' +
                           (f' Tmap({die}, "temperature_map.txt", slot);\n' if dense_maps else ""),
                           source, flags=re.S)
    if count != 1:
        raise ValueError("missing stack output section")
    output = Path(output)
    (output / "ice.stk").write_text(source)
    (output / "floorplan.flp").write_bytes((Path(recording) / "floorplan.flp").read_bytes())
    return step_ps, slot_ps


class IceClient:
    def __init__(self, server, directory, timeout=180.):
        self.server = str(Path(server).resolve())
        self.directory = Path(directory)
        self.timeout = timeout
        self.process = self.sock = self.log = None

    def __enter__(self):
        try:
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            environment = os.environ.copy()
            environment.setdefault("OPENBLAS_NUM_THREADS", "1")
            self.log = (self.directory / "server.log").open("w")
            self.process = subprocess.Popen([self.server, "ice.stk", str(port)],
                                            cwd=self.directory, env=environment, stdout=self.log,
                                            stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + self.timeout
            while self.sock is None:
                if self.process.poll() is not None:
                    raise RuntimeError(f"3D-ICE exited during startup; see {self.directory / 'server.log'}")
                try:
                    self.sock = socket.create_connection(("127.0.0.1", port), timeout=.2)
                    # Before the server listens, Linux can assign the client
                    # this same ephemeral port and connect it to itself. Such
                    # a socket echoes requests instead of returning replies.
                    if self.sock.getsockname() == self.sock.getpeername():
                        self.sock.close()
                        self.sock = None
                except (ConnectionRefusedError, TimeoutError):
                    pass
                if self.sock is None:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("3D-ICE startup timed out")
                    time.sleep(.02)
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
        if len(payload) < 20:
            raise RuntimeError("missing Tflp inspection point")
        _, count, filename_len, header_len, width = struct.unpack_from("=fIIII", payload)
        if count != 1 or width != len(expected_entries):
            raise RuntimeError("unexpected Tflp output count or width")
        offset = 20 + ((filename_len + 3) // 4) * 4
        header = payload[offset:offset + header_len].decode()
        names = re.findall(r"([^\s]+)\(K\)", header)
        if names != expected_entries:
            raise RuntimeError("Tflp order differs from the recording floorplan")
        offset += ((header_len + 3) // 4) * 4
        if len(payload) != offset + 4 * width:
            raise RuntimeError("malformed Tflp payload")
        values = np.frombuffer(payload, dtype="=f4", offset=offset).astype(float)
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
