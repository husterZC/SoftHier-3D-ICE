"""Stop a recorded coupled process and its current descendants."""
import os
from pathlib import Path
import signal
import sys
import time


def identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except FileNotFoundError:
        return None


def stop(pid):
    processes = {}
    def collect(current):
        token = identity(current)
        if token is None or current in processes:
            return
        processes[current] = token
        try:
            children = Path(f"/proc/{current}/task/{current}/children").read_text().split()
        except FileNotFoundError:
            children = []
        for child in children:
            collect(int(child))
    collect(pid)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for current, token in reversed(list(processes.items())):
            if identity(current) == token:
                try:
                    os.kill(current, sig)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 2
        while any(identity(p) == token for p, token in processes.items()) and time.monotonic() < deadline:
            time.sleep(.02)


if __name__ == "__main__":
    stop(int(sys.argv[1]))
