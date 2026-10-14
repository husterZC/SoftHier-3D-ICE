import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

NATIVE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("apply_native", NATIVE / "apply_native.py")
apply_native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply_native)
REPO = apply_native.REPO


class NativePatchTests(unittest.TestCase):
    def test_exact_source_idempotence_and_modified_source_rejection(self):
        manifest = json.loads((NATIVE / "patch_manifest.json").read_text())
        source = REPO / "SoftHier"
        files = manifest["groups"]["softhier"]["files"]
        if not all((source / path).is_file() for path in files):
            self.skipTest("pinned SoftHier submodules are not populated")
        workspace = REPO / "tmp" / "native_patch_tests"
        workspace.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(workspace)) as directory:
            stage = Path(directory)
            for relative in files:
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / relative, target)
            first = apply_native.apply("softhier", stage)
            second = apply_native.apply("softhier", stage)
            self.assertEqual(first, second)
            changed = stage / next(iter(files))
            changed.write_text(changed.read_text() + "\n// local modification\n")
            with self.assertRaisesRegex(ValueError, "unsupported source"):
                apply_native.apply("softhier", stage)


if __name__ == "__main__":
    unittest.main()
