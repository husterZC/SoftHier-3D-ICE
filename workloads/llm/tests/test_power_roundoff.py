"""Exercise the actual engine update method with exact-zero and invalid inputs."""
import ctypes
from pathlib import Path
import subprocess
import tempfile
import unittest

PACKAGE = Path(__file__).resolve().parents[1]
SOURCE = PACKAGE.parents[1] / 'SoftHier/engine/engine/src/power/power_trace.cpp'


class PowerRoundoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        original = SOURCE.read_text()
        (root / 'power_trace.cpp').write_text(original)
        subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i',
                        str(PACKAGE / 'simulator/0003-zero-idle-power-roundoff.patch')],
                       cwd=root, check=True, stdout=subprocess.DEVNULL)
        cls.libs = []
        for name, text in [('original', original), ('patched', (root / 'power_trace.cpp').read_text())]:
            start = text.index('void vp::PowerTrace::inc_dynamic_power(')
            end = text.index('void vp::PowerTrace::inc_leakage_power(', start)
            # Only the surrounding event/trace interfaces are stubbed. Compile
            # the real power accumulation method before and after the patch.
            source = '''
#include <stdint.h>
namespace vp {
class PowerTrace {
public:
    double current_dynamic_power = 0;
    PowerTrace *parent = nullptr;
    void inc_dynamic_power(double);
    void account_dynamic_power() {}
    void dump_vcd_trace() {}
};
}
''' + text[start:end] + '''
extern "C" double accumulate(const double *values, uint32_t count) {
    vp::PowerTrace trace;
    for (uint32_t i = 0; i < count; ++i) trace.inc_dynamic_power(values[i]);
    return trace.current_dynamic_power;
}
'''
            path = root / (name + '.cpp')
            path.write_text(source)
            library = root / (name + '.so')
            subprocess.run(['c++', '-O3', '-shared', '-fPIC', '-Wall', '-Werror',
                            str(path), '-o', str(library)], check=True)
            lib = ctypes.CDLL(str(library))
            lib.accumulate.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_uint32]
            lib.accumulate.restype = ctypes.c_double
            cls.libs.append(lib)

    def accumulation(self, index, values):
        array = (ctypes.c_double * len(values))(*values)
        return self.libs[index].accumulate(array, len(values))

    def test_idle_roundoff_cannot_decrease_total_energy(self):
        values = [0.3] * 128 + [-0.1, -0.2] * 128
        previous = self.accumulation(0, values)
        self.assertLess(previous * 10000000, -1e-9)
        self.assertEqual(self.accumulation(1, values), 0.0)

    def test_real_negative_power_still_reaches_accounting_checks(self):
        self.assertEqual(self.accumulation(1, [-1e-6]), -1e-6)

    def test_positive_power_is_preserved_including_small_values(self):
        for values in ([1e-15], [0.5, -0.25], [0.3, 0.1, -0.2]):
            self.assertEqual(self.accumulation(1, values), self.accumulation(0, values))
