#!/usr/bin/env python3
"""Wait until a log file contains a pattern."""

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wait for a pattern to appear in a log file.")
    parser.add_argument("--file", required=True, help="Log file to read.")
    parser.add_argument("--pattern", required=True, help="Substring to wait for.")
    parser.add_argument("--timeout", type=float, default=120.0, help="Timeout in seconds.")
    parser.add_argument("--pid", type=int, help="Optional process PID that must stay alive.")
    parser.add_argument("--poll", type=float, default=0.2, help="Polling interval in seconds.")
    return parser.parse_args()


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    stat_path = Path("/proc") / str(pid) / "stat"
    if stat_path.exists():
        try:
            stat = stat_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return True

        end_comm = stat.rfind(")")
        if end_comm != -1:
            fields = stat[end_comm + 1 :].strip().split()
            if fields and fields[0] == "Z":
                return False

    return True


def wait_for_log(path: Path, pattern: str, timeout: float, pid: Optional[int], poll: float) -> int:
    deadline = time.monotonic() + timeout
    offset = 0

    while time.monotonic() < deadline:
        if pid is not None and not process_alive(pid):
            print(f"wait_for_log.py: process {pid} exited before pattern appeared", file=sys.stderr)
            return 2

        if path.exists():
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                stream.seek(offset)
                data = stream.read()
                offset = stream.tell()

            if pattern in data:
                return 0

        time.sleep(poll)

    print(f"wait_for_log.py: timed out waiting for {pattern!r} in {path}", file=sys.stderr)
    return 1


def main() -> int:
    args = parse_args()
    return wait_for_log(Path(args.file), args.pattern, args.timeout, args.pid, args.poll)


if __name__ == "__main__":
    raise SystemExit(main())
