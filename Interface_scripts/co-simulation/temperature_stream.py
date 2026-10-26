"""Bounded, incremental reader for append-only native Tflp output."""
import math
import os
from pathlib import Path


class TemperatureStream:
    def __init__(self, path, entries):
        self.path = Path(path)
        self.entries = list(entries)
        self.stream = None
        self.identity = None
        self.buffer = bytearray()
        self.scan = 0
        self.rows = self.bytes_read = self.checks = self.peak_buffer_bytes = 0
        self.max_line_bytes = max(65536, 256 * (len(entries) + 1))

    def _open_check(self):
        self.checks += 1
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            if self.stream is not None:
                raise ValueError(f"temperature file disappeared: {self.path}")
            return False
        identity = (stat.st_dev, stat.st_ino)
        if self.stream is None:
            self.stream = self.path.open("rb", buffering=0)
            actual = os.fstat(self.stream.fileno())
            self.identity = (actual.st_dev, actual.st_ino)
        if identity != self.identity or stat.st_size < self.stream.tell():
            raise ValueError(f"temperature file replaced or truncated: {self.path}")
        return True

    def read_row(self):
        """Return one new complete row, or None; incomplete bytes stay buffered."""
        if not self._open_check():
            return None
        while True:
            end = self.buffer.find(b"\n", self.scan)
            if end < 0:
                self.scan = len(self.buffer)
                if len(self.buffer) > self.max_line_bytes:
                    raise ValueError(f"temperature row is too large: {self.path}")
                chunk = self.stream.read(65536)
                if not chunk:
                    return None
                self.bytes_read += len(chunk)
                self.buffer.extend(chunk)
                self.peak_buffer_bytes = max(self.peak_buffer_bytes, len(self.buffer))
                continue
            if end > self.max_line_bytes:
                raise ValueError(f"temperature row is too large: {self.path}")
            line = self.buffer[:end].decode("utf-8").strip()
            del self.buffer[:end + 1]
            self.scan = 0
            if not line:
                continue
            fields = line.split()
            if line.startswith("%"):
                if len(fields) >= 2 and fields[1] == "Time(s)":
                    if any(not item.endswith("(K)") for item in fields[2:]):
                        raise ValueError(f"malformed Tflp header: {self.path}")
                    if [item[:-3] for item in fields[2:]] != self.entries:
                        raise ValueError(f"Tflp header does not match generated floorplan order: {self.path}")
                continue
            if len(fields) != len(self.entries) + 1:
                raise ValueError(f"wrong temperature row width: {self.path}")
            values = [float(item) for item in fields]
            if not all(math.isfinite(value) for value in values):
                raise ValueError(f"non-finite Tflp value: {self.path}")
            if any(value <= 0 for value in values[1:]):
                raise ValueError(f"temperatures must be positive Kelvin: {self.path}")
            self.rows += 1
            return values[0], values[1:]

    def diagnostics(self):
        return dict(path=str(self.path), rows_parsed=self.rows, bytes_read=self.bytes_read,
                    checks=self.checks, peak_buffer_bytes=self.peak_buffer_bytes)

    def close(self):
        if self.stream is not None:
            self.stream.close()
