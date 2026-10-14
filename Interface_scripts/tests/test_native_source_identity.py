"""Build cache identity covers all source modules and stays inside the repo."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Interface_scripts/providers/softhier"))
import stage_native


class NativeSourceIdentityTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / "tmp/interface_tests"
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=str(base))
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.source = self.folder / "source"
        self.create_repo(self.source)
        for name in ("engine", "core", "pulp", "gvrun", "pulpos", "config_tree", "gvtest"):
            self.create_repo(self.source / name)
        self.create_repo(self.source / "gvrun/nested")
        self.add_gitlink(self.source / "gvrun", "nested")
        for name in ("engine", "core", "pulp", "gvrun", "pulpos", "config_tree", "gvtest"):
            self.add_gitlink(self.source, name)

    @staticmethod
    def git(path, *args):
        return subprocess.check_output(["git", "-C", str(path)] + list(args), stderr=subprocess.STDOUT).decode().strip()

    def create_repo(self, path):
        path.mkdir(parents=True)
        self.git(path, "init", "-q")
        self.git(path, "config", "user.name", "Cache test")
        self.git(path, "config", "user.email", "cache-test@example.invalid")
        (path / "source.txt").write_text("original source\n")
        self.git(path, "add", "source.txt")
        self.git(path, "commit", "-qm", "fixture")

    def add_gitlink(self, parent, name):
        commit = self.git(parent / name, "rev-parse", "HEAD")
        self.git(parent, "update-index", "--add", "--cacheinfo", "160000", commit, name)
        self.git(parent, "commit", "-qm", "add " + name)

    def test_all_modules_and_nested_commit_changes_affect_identity(self):
        original = stage_native.source_identity(self.source)
        self.assertEqual(set(original["bases"]),
                         {".", "engine", "core", "pulp", "gvrun", "pulpos", "config_tree", "gvtest", "gvrun/nested"})
        for name in ("gvrun", "pulpos", "config_tree", "gvrun/nested"):
            module = self.source / name
            (module / "source.txt").write_text("changed source for {}\n".format(name))
            self.git(module, "commit", "-qam", "change source")
            with self.subTest(module=name):
                changed = stage_native.source_identity(self.source)
                self.assertNotEqual(changed["bases"][name], original["bases"][name])

    def test_dirty_shared_or_nested_source_is_rejected(self):
        for name in ("gvrun", "pulpos", "config_tree", "gvrun/nested"):
            source_file = self.source / name / "source.txt"
            source_file.write_text("uncommitted change\n")
            with self.subTest(module=name), self.assertRaisesRegex(ValueError, "tracked source edits"):
                stage_native.source_identity(self.source)
            source_file.write_text("original source\n")

    def test_uninitialized_module_cannot_fall_back_to_parent_repository(self):
        shutil.rmtree(str(self.source / "config_tree/.git"))
        with self.assertRaisesRegex(ValueError, "uninitialized source module"):
            stage_native.source_identity(self.source)

    def test_paths_are_checked_before_accessing_source_or_systemc(self):
        # Narrow the allowed root to a fixture to simulate outside paths while
        # all files used by the test still remain inside the real repository.
        allowed = self.folder / "allowed"
        allowed.mkdir()
        with patch.object(stage_native, "REPO", allowed):
            with self.assertRaises(ValueError):
                stage_native.source_identity(self.source)
            with self.assertRaises(ValueError):
                stage_native.systemc_identity(self.source, "3.0.1")
            systemc = allowed / "systemc"
            (systemc / "lib64").mkdir(parents=True)
            (systemc / "lib64/libsystemc.so").symlink_to(self.source / "source.txt")
            with self.assertRaises(ValueError):
                stage_native.systemc_identity(systemc, "3.0.1")


if __name__ == "__main__":
    unittest.main()
