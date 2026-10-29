"""User-space CPU sampling through Linux perf_event_open (no perf executable).

The collector records instruction pointers, not call stacks. Percentages are
exclusive samples, including unresolved samples in the denominator. x86-64's
ordered loads/stores supply the perf ring's acquire/release ordering here.
"""
from collections import Counter, defaultdict
import bisect
import ctypes
import fcntl
import mmap
import os
from pathlib import Path
import platform
import struct
import subprocess


class Sampler:
    def __init__(self, pid, interval_ns=10_000_000):
        if platform.machine() != "x86_64":
            raise RuntimeError("native sampler currently requires Linux x86-64")
        self.pid, self.rings = pid, []
        self.samples, self.lost = Counter(), 0
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            for task in sorted(Path(f"/proc/{pid}/task").iterdir()):
                attr = ctypes.create_string_buffer(128)
                # SOFTWARE / CPU_CLOCK; IP + TID; disabled, exclude kernel/HV.
                struct.pack_into("IIQQQQQ", attr, 0, 1, 128, 0, interval_ns,
                                 1 | 2, 0, 1 | (1 << 5) | (1 << 6))
                fd = libc.syscall(298, ctypes.byref(attr), int(task.name), -1, -1, 0)
                if fd < 0:
                    raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
                try:
                    ring = mmap.mmap(fd, mmap.PAGESIZE * 129, flags=mmap.MAP_SHARED,
                                     prot=mmap.PROT_READ | mmap.PROT_WRITE)
                except BaseException:
                    os.close(fd)
                    raise
                self.rings.append((fd, ring))
        except BaseException:
            self.close()
            raise

    def start(self):
        for fd, _ in self.rings:
            fcntl.ioctl(fd, 0x2403, 0)  # RESET
            fcntl.ioctl(fd, 0x2400, 0)  # ENABLE

    def stop(self):
        for fd, _ in self.rings:
            fcntl.ioctl(fd, 0x2401, 0)  # DISABLE
        self.drain()

    def drain(self):
        for _, ring in self.rings:
            head, tail, offset, size = struct.unpack_from("QQQQ", ring, 1024)
            if head - tail > size:
                raise RuntimeError("sampling ring overflow; shorten the profiling window")
            def read(position, length):
                begin = position % size
                first = min(length, size - begin)
                return ring[offset + begin:offset + begin + first] + ring[offset:offset + length - first]
            while tail < head:
                kind, _, length = struct.unpack("IHH", read(tail, 8))
                if length < 8 or tail + length > head:
                    raise RuntimeError("invalid perf record")
                data = read(tail + 8, length - 8)
                if kind == 9:  # PERF_RECORD_SAMPLE
                    ip, pid, tid = struct.unpack_from("QII", data)
                    self.samples[(pid, tid, ip)] += 1
                elif kind == 2:  # PERF_RECORD_LOST
                    self.lost += struct.unpack_from("QQ", data)[1]
                tail += length
            struct.pack_into("Q", ring, 1032, tail)

    def close(self):
        for fd, ring in self.rings:
            ring.close()
            os.close(fd)
        self.rings = []


def parse_maps(text):
    result = []
    for line in text.splitlines():
        fields = line.split(None, 5)
        if len(fields) < 6 or "x" not in fields[1]:
            continue
        start, end = (int(x, 16) for x in fields[0].split("-"))
        result.append((start, end, int(fields[2], 16), fields[5]))
    return result


def category(symbol, module):
    if any(x in symbol for x in ("PowerTrace::", "PowerSource::", "BlockPower::", "PowerEngine::")):
        return "logic_power"
    if "softhier_hbm2" in symbol or "DRAMPower::" in symbol:
        return "dram_power"
    if "libDRAMSys" in module:
        return "dram_simulation_other"
    if "ClockEngine::" in symbol or "ClockEvent::" in symbol or "TimeEngine::" in symbol:
        return "event_scheduling"
    return "other"


def symbolize(samples, maps_text):
    maps = parse_maps(maps_text)
    symbols, starts = {}, {}
    functions = Counter()
    total = sum(samples.values())
    for (_, _, ip), count in samples.items():
        mapping = next((m for m in maps if m[0] <= ip < m[1]), None)
        if mapping is None:
            functions[("[unmapped]", hex(ip))] += count
            continue
        start, _, offset, path = mapping
        if path not in symbols:
            entries = []
            if Path(path).is_file():
                result = subprocess.run(["nm", "--defined-only", "-n", "-S", "-C", path],
                                        capture_output=True, text=True, check=False)
                for line in result.stdout.splitlines():
                    fields = line.split(None, 3)
                    if len(fields) == 4 and fields[2] in "tTwWiI":
                        try:
                            entries.append((int(fields[0], 16), int(fields[1], 16), fields[3]))
                        except ValueError:
                            pass
            entries.sort()
            symbols[path] = entries
            starts[path] = [entry[0] for entry in entries]
        address = ip - start + offset
        if Path(path).is_file():
            with open(path, "rb") as stream:
                header = stream.read(18)
            if header[:4] == b"\x7fELF" and struct.unpack_from("H", header, 16)[0] == 2:
                address = ip  # ET_EXEC addresses are absolute.
        index = bisect.bisect_right(starts[path], address) - 1
        name = f"[unresolved]+{address:#x}"
        if index >= 0:
            base, length, candidate = symbols[path][index]
            if length and address < base + length:
                name = candidate
        functions[(path, name)] += count
    rows, categories = [], defaultdict(int)
    for (module, function), count in functions.most_common():
        group = category(function, module)
        categories[group] += count
        rows.append(dict(module=module, function=function, samples=count,
                         percent=100 * count / total if total else 0, category=group))
    return dict(sample_count=total, functions=rows,
                categories={k: dict(samples=v, percent=100 * v / total if total else 0)
                            for k, v in categories.items()},
                method="100 Hz per-thread user CPU instruction-pointer samples; exclusive, not call-stack attribution")
