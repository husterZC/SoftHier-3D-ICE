"""Private DRAM power settings must not alter another memory's configuration."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Interface_scripts/providers/softhier"))
import prepare_dram_config as staging


class DramConfigStagingTests(unittest.TestCase):
    def test_power_enable_preserves_shared_settings_and_invalidates_changed_inputs(self):
        base = ROOT / "tmp/interface_tests"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as temporary:
            folder = Path(temporary)
            source, work = folder / "source", folder / "work"
            configs = source / "dramsys_configs"
            (configs / "simconfig").mkdir(parents=True)
            original = {"simconfig": {"PowerAnalysis": False, "StoreMode": "Store"}}
            settings = configs / "simconfig/example.json"
            settings.write_text(json.dumps(original))
            simulation = {"simulation": {"simconfig": "simconfig/example.json"}}
            for name in ("hbm2-example.json", "offchip.json"):
                (configs / name).write_text(json.dumps(simulation))
            with patch.object(staging, "import_architecture", return_value=SimpleNamespace(dram3d_enable=1)):
                result = staging.prepare(folder / "arch.py", source, work)
                self.assertEqual(result, staging.prepare(folder / "arch.py", source, work))
                self.assertEqual(json.loads(settings.read_text()), original)
                copied = result / "dramsys_configs"
                self.assertEqual(json.loads((copied / "offchip.json").read_text()), simulation)
                self.assertEqual(json.loads((copied / "simconfig/example.json").read_text()), original)
                selected = json.loads((copied / "hbm2-example.json").read_text())["simulation"]["simconfig"]
                self.assertTrue(json.loads((copied / selected).read_text())["simconfig"]["PowerAnalysis"])
                settings.write_text(json.dumps({"simconfig": {"PowerAnalysis": False, "StoreMode": "NoStorage"}}))
                self.assertNotEqual(result, staging.prepare(folder / "arch.py", source, work))
            with patch.object(staging, "import_architecture", return_value=SimpleNamespace(dram3d_enable=0)):
                self.assertEqual(staging.prepare(folder / "arch.py", source, work), source)


if __name__ == "__main__":
    unittest.main()
