#!/usr/bin/env python3
"""Run-local persistent adapter, addressed by the unchanged executable hook."""
import argparse
import importlib
import json
import os
from pathlib import Path
import resource
import signal
import socket
import struct
import sys

HOOK = importlib.import_module("3dice_power_hook")


def receive(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("hook client disconnected before completing request")
        data.extend(chunk)
    return bytes(data)


def check_processes(pids):
    for pid in pids:
        try:
            os.kill(pid, 0)
            stat = Path(f"/proc/{pid}/stat")
            if stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                raise ProcessLookupError(pid)
        except ProcessLookupError as exc:
            raise RuntimeError(f"coupled process {pid} exited") from exc


def serve(args):
    session = None
    owns_socket = ready_written = False
    status, error, requests = "failed", None, 0
    socket_path = Path(args.socket)
    expected = [str(Path(getattr(args, key)).resolve()) for key in ("config", "request", "response")]
    health = lambda: check_processes(args.watch_pid)
    try:
        if socket_path.exists():
            raise RuntimeError("worker socket already exists; use a fresh run")
        session = HOOK.HookSession(expected[0], health)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            owns_socket = True
            os.chmod(socket_path, 0o600)
            server.listen(1)
            server.settimeout(.2)
            HOOK.atomic_json(Path(args.ready), dict(pid=os.getpid(), socket=str(socket_path)))
            ready_written = True
            print("Power hook worker ready", flush=True)
            while not session.finished:
                health()
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(session.config["timeout_seconds"])
                    try:
                        length, = struct.unpack("!I", receive(connection, 4))
                        if not 1 <= length <= 65536:
                            raise ValueError("invalid hook client request length")
                        fields = receive(connection, length).decode("utf-8").split("\0")
                        if len(fields) != 5 or fields[-1] or fields[1:4] != expected:
                            raise ValueError("hook client paths differ from this run")
                        phase = fields[0]
                        if phase not in ("init", "update", "final"):
                            raise ValueError("invalid hook phase")
                        call = argparse.Namespace(phase=phase, config=expected[0],
                                                  request=expected[1], response=expected[2])
                        HOOK.run(call, session=session)
                        requests += 1
                        connection.sendall(struct.pack("!II", 0, 0))
                    except Exception as exc:
                        message = str(exc).encode("utf-8")[:4096]
                        try:
                            connection.sendall(struct.pack("!II", 1, len(message)) + message)
                        except OSError:
                            pass
                        raise
        status = "complete"
    except BaseException as exc:
        error = str(exc) or type(exc).__name__
        raise
    finally:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        diagnostics = dict(status=status, error=error, pid=os.getpid(), requests=requests,
                           configuration_loads=1 if session else 0,
                           exchanges=session.exchanges if session else 0,
                           user_seconds=usage.ru_utime, system_seconds=usage.ru_stime,
                           readers=[r.diagnostics() for r in session.readers] if session else [])
        if session:
            session.close()
        HOOK.atomic_json(Path(args.diagnostics), diagnostics)
        HOOK.overhead_emit("adapter", "process_exit", HOOK.overhead_now())
        if owns_socket:
            socket_path.unlink(missing_ok=True)
        if ready_written:
            Path(args.ready).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("socket", "ready", "diagnostics", "config", "request", "response"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--watch-pid", action="append", type=int, default=[])
    args = parser.parse_args()
    def terminate(signum, _frame):
        raise RuntimeError(f"worker interrupted by signal {signum}")
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        serve(args)
    except BaseException as exc:
        print(f"power hook worker: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
