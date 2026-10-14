"""Installed native artifacts must match their pinned source and patch identity."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Interface_scripts/providers/softhier"))
import native_build_cache as dram_cache
import stage_native as gv_cache


class NativeBuildCacheTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / "tmp/interface_tests"
        base.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.systemc = self.folder / "systemc"
        (self.systemc / "lib64").mkdir(parents=True)
        (self.systemc / "lib64/libsystemc.so").write_bytes(b"SystemC A")

    def test_dram_base_patch_systemc_and_library_changes_invalidate_cache(self):
        patch_file = self.folder / "upstream.patch"
        patch_file.write_text("integration patch A")
        systemc = self.systemc
        systemc_library = systemc / "lib64/libsystemc.so"
        systemc_library.write_bytes(b"SystemC A")
        install = self.folder / "dram"
        install.mkdir()
        library = install / "libDRAMSys_Simulator.so"
        library.write_bytes(b"DRAMSys A")
        with patch.object(dram_cache, "fingerprint", return_value="bundle A"):
            original = dram_cache.identity("base A", patch_file, systemc, "3.0.1")
            dram_cache.stamp_or_check(original, install)
            dram_cache.stamp_or_check(original, install, check=True)
            for base_commit, version in (("base B", "3.0.1"), ("base A", "3.0.2")):
                with self.subTest(base=base_commit, version=version), self.assertRaisesRegex(ValueError, "differ"):
                    dram_cache.stamp_or_check(dram_cache.identity(base_commit, patch_file, systemc, version), install, check=True)
            patch_file.write_text("integration patch B")
            changed_patch = dram_cache.identity("base A", patch_file, systemc, "3.0.1")
            self.assertNotEqual(dram_cache.cache_key(original), dram_cache.cache_key(changed_patch))
            with self.assertRaisesRegex(ValueError, "differ"):
                dram_cache.stamp_or_check(changed_patch, install, check=True)
            patch_file.write_text("integration patch A")
            systemc_library.write_bytes(b"SystemC B")
            with self.assertRaisesRegex(ValueError, "differ"):
                dram_cache.stamp_or_check(dram_cache.identity("base A", patch_file, systemc, "3.0.1"), install, check=True)
            library.write_bytes(b"different DRAMSys artifact")
            with self.assertRaisesRegex(ValueError, "differ"):
                dram_cache.stamp_or_check(original, install, check=True)

    def test_gvsoc_stamp_rejects_changed_base_target_and_installed_artifact(self):
        work = self.folder / "work"
        stage = work / "native_sources/test-stage"
        stage.mkdir(parents=True)
        identity = {"bases": {".": "SoftHier A", "engine": "engine A", "core": "core A", "pulp": "pulp A"},
                    "softhier_patch": "patch A", "stager_sha256": "stager A"}
        (stage / "stage.json").write_text(json.dumps(identity))
        for name in gv_cache.BUILD_ARTIFACTS:
            path = work / "install" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture dram_get_core_energy_j" if name == "models/profile/memory/dramsys.so"
                             else b"normal binary may compile out power")
        symbols = b"000000000000 T vp::BlockPower::get_external_energy(long, double*, double*)\n"
        with patch.object(gv_cache, "source_identity", return_value=identity), \
             patch.object(gv_cache.subprocess, "check_output", return_value=symbols) as symbol_reader, \
             patch.object(gv_cache, "apply"):
            stamped = gv_cache.build_stamp(self.folder, work, "target A", self.systemc, "3.0.1", stage)
            self.assertEqual(symbol_reader.call_args[0][0][-1], str(work / "install/lib/libpulpvp-profile.so"))
            self.assertEqual(stamped["staged_source"], str(stage))
            self.assertIn("bin/gvsoc_launcher_profile_sc", stamped["artifacts_sha256"])
            self.assertIn("models/profile/memory/dramsys.so", stamped["artifacts_sha256"])
            gv_cache.build_stamp(self.folder, work, "target A", self.systemc, "3.0.1", check=True)
            with self.assertRaisesRegex(ValueError, "target differ"):
                gv_cache.build_stamp(self.folder, work, "target B", self.systemc, "3.0.1", check=True)
            changed = dict(identity, bases=dict(identity["bases"], engine="engine B"))
            with patch.object(gv_cache, "source_identity", return_value=changed), self.assertRaisesRegex(ValueError, "sources/patches/target"):
                gv_cache.build_stamp(self.folder, work, "target A", self.systemc, "3.0.1", check=True)
            self.assertEqual(stamped["identity"]["systemc"], gv_cache.systemc_identity(self.systemc, "3.0.1"))
            with self.assertRaisesRegex(ValueError, "SystemC changed"):
                gv_cache.build_stamp(self.folder, work, "target A", self.systemc, "3.0.2", check=True)
            other_systemc = self.folder / "other-systemc"
            (other_systemc / "lib64").mkdir(parents=True)
            (other_systemc / "lib64/libsystemc.so").write_bytes(b"SystemC A")
            with self.assertRaisesRegex(ValueError, "SystemC changed"):
                gv_cache.build_stamp(self.folder, work, "target A", other_systemc, "3.0.1", check=True)
            (self.systemc / "lib64/libsystemc.so").write_bytes(b"SystemC B")
            with self.assertRaisesRegex(ValueError, "SystemC changed"):
                gv_cache.build_stamp(self.folder, work, "target A", self.systemc, "3.0.1", check=True)
            (self.systemc / "lib64/libsystemc.so").write_bytes(b"SystemC A")
            (work / "install/bin/gvsoc").write_bytes(b"another executable")
            with self.assertRaisesRegex(ValueError, "artifacts differ"):
                gv_cache.build_stamp(self.folder, work, "target A", self.systemc, "3.0.1", check=True)

    def test_gvsoc_capabilities_are_checked_before_accepting_a_stamp(self):
        work = self.folder / "work"
        for name in gv_cache.BUILD_ARTIFACTS:
            path = work / "install" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"old binary without new ABI")
        with patch.object(gv_cache.subprocess, "check_output", return_value=b"old engine symbols"):
            with self.assertRaisesRegex(ValueError, "external-energy callback"):
                gv_cache.installed_artifacts(work)
        with patch.object(gv_cache.subprocess, "check_output", return_value=b" T vp::BlockPower::get_external_energy("):
            with self.assertRaisesRegex(ValueError, "cumulative-energy ABI"):
                gv_cache.installed_artifacts(work)


if __name__ == "__main__":
    unittest.main()
