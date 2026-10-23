"""Opt-in host spans; the event schema is shared with closed_loop_timing.h."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import resource
import time

DIRECTORY = os.environ.get("CLO_TIMING_DIR")


def now():
    return time.monotonic_ns() if DIRECTORY else 0


def emit(actor, phase, start, end=None, sim_ps=0, value=0):
    if not DIRECTORY:
        return
    usage = resource.getrusage(resource.RUSAGE_SELF)
    record = dict(actor=actor, phase=phase, pid=os.getpid(), start_ns=start,
                  end_ns=now() if end is None else end, sim_ps=sim_ps, value=value,
                  user_us=round(usage.ru_utime * 1e6), system_us=round(usage.ru_stime * 1e6),
                  maxrss_kb=usage.ru_maxrss)
    with (Path(DIRECTORY) / f"{os.getpid()}.jsonl").open("a") as stream:
        stream.write(json.dumps(record, separators=(",", ":")) + "\n")


@contextmanager
def span(actor, phase, sim_ps=0):
    start = now()
    try:
        yield
    finally:
        emit(actor, phase, start, sim_ps=sim_ps)
