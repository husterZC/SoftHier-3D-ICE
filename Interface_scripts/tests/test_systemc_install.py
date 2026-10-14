"""A requested SystemC release must match both headers and the exported ABI."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Interface_scripts/providers/softhier"))
import systemc_install


class SystemCInstallTests(unittest.TestCase):
    def test_rejects_stale_headers_or_library(self):
        base = ROOT / "tmp/interface_tests"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as folder:
            home = Path(folder)
            header = home / "include/sysc/kernel/sc_ver.h"
            header.parent.mkdir(parents=True)
            header.write_text("#define SC_VERSION_MAJOR 3\n#define SC_VERSION_MINOR 0\n#define SC_VERSION_PATCH 1\n")
            (home / "include/systemc.h").touch()
            (home / "lib64").mkdir()
            (home / "lib64/libsystemc.so").touch()
            with patch.object(systemc_install.subprocess, "check_output", return_value=" T sc_core::sc_api_version_3_0_1_cxx201703L::sc_api_version_3_0_1_cxx201703L("):
                systemc_install.validate(home, "3.0.1")
                with self.assertRaisesRegex(ValueError, "headers are 3.0.1"):
                    systemc_install.validate(home, "3.0.2")
            with patch.object(systemc_install.subprocess, "check_output", return_value=" T sc_core::sc_api_version_2_3_4_cxx201703L::sc_api_version_2_3_4_cxx201703L("):
                with self.assertRaisesRegex(ValueError, "header ABI"):
                    systemc_install.validate(home, "3.0.1")


if __name__ == "__main__":
    unittest.main()
