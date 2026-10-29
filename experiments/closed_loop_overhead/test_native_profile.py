"""Check perf ring decoding and ELF address attribution without perf permissions."""
from collections import Counter
import mmap
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

from .native_profile import Sampler, symbolize


class RingTests(unittest.TestCase):
    def ring(self, records, tail=0, size=128):
        ring = mmap.mmap(-1, 4096 + size)
        self.addCleanup(ring.close)
        struct.pack_into("QQQQ", ring, 1024, tail + len(records), tail, 4096, size)
        for i, byte in enumerate(records):
            ring[4096 + (tail + i) % size] = byte
        sampler = Sampler.__new__(Sampler)
        sampler.rings, sampler.samples, sampler.lost = [(None, ring)], Counter(), 0
        return sampler, ring

    def test_sample_and_lost_records_across_wrap(self):
        sample = struct.pack("IHHQII", 9, 0, 24, 0x12345678, 13, 17)
        lost = struct.pack("IHHQQ", 2, 0, 24, 5, 9)
        sampler, ring = self.ring(sample + lost + sample, tail=119)
        sampler.drain()
        self.assertEqual(sampler.samples, {(13, 17, 0x12345678): 2})
        self.assertEqual(sampler.lost, 9)
        self.assertEqual(struct.unpack_from("Q", ring, 1032)[0], 119 + 72)
        sampler.drain()
        self.assertEqual(sum(sampler.samples.values()), 2)

    def test_overflow_is_not_silently_accepted(self):
        sampler, ring = self.ring(b"")
        struct.pack_into("Q", ring, 1024, 129)
        with self.assertRaisesRegex(RuntimeError, "overflow"):
            sampler.drain()

    def test_incomplete_record_is_rejected(self):
        sampler, _ = self.ring(struct.pack("IHH", 9, 0, 24))
        with self.assertRaisesRegex(RuntimeError, "invalid perf record"):
            sampler.drain()


@unittest.skipUnless(shutil.which("cc") and shutil.which("nm"), "requires native compiler and nm")
class AttributionTests(unittest.TestCase):
    def test_pie_and_fixed_address_binaries_with_unknown_samples(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "test.c"
            source.write_text("__attribute__((noinline)) int sample_target(int x) { return x + 1; }\n"
                              "int main(void) { return sample_target(3); }\n")
            for pie in (False, True):
                binary = directory / ("pie" if pie else "fixed")
                subprocess.run(["cc", "-O0", "-fPIE" if pie else "-fno-pie",
                                "-pie" if pie else "-no-pie", str(source), "-o", str(binary)], check=True)
                symbols = subprocess.check_output(["nm", "-S", str(binary)], text=True)
                address = int(next(line.split()[0] for line in symbols.splitlines()
                                   if line.endswith(" sample_target")), 16)
                relocation = 0x70000000 if pie else 0
                start = relocation + (address // 4096) * 4096
                ip = relocation + address
                maps = f"{start:x}-{start + 4096:x} r-xp 00001000 00:00 1 {binary}\n"
                report = symbolize(Counter({(13, 17, ip): 3, (13, 17, 0xdeadbeef): 1}), maps)
                target = next(row for row in report["functions"] if row["function"] == "sample_target")
                self.assertEqual(report["sample_count"], 4)
                self.assertEqual(target["percent"], 75)
                self.assertTrue(any(row["module"] == "[unmapped]" for row in report["functions"]))


if __name__ == "__main__":
    unittest.main()
