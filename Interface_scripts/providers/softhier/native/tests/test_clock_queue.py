"""Compile actual queue methods and compare their complete observable traces."""
import importlib.util
from pathlib import Path
import re
import resource
import shutil
import subprocess
import tempfile
import unittest

NATIVE = Path(__file__).resolve().parents[1]
ROOT = NATIVE.parents[3]
spec = importlib.util.spec_from_file_location("apply_native_queue", NATIVE / "apply_native.py")
apply_native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply_native)


def method(text, signature):
    start = text.index(signature)
    depth = 0
    for token in re.finditer(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|[{}]', text[start:], re.S):
        if token.group() == "{":
            depth += 1
        elif token.group() == "}":
            depth -= 1
            if depth == 0:
                return text[start:start + token.end()]
    raise ValueError("unterminated native method: " + signature)


@unittest.skipUnless(shutil.which("g++"), "requires native compiler")
class ClockQueueTests(unittest.TestCase):
    def test_original_and_cached_scheduler_have_identical_traces(self):
        import json
        workspace = ROOT / "tmp/clock_queue_tests"
        workspace.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=workspace) as temporary:
            folder = Path(temporary)
            stage = folder / "stage"
            manifest = json.loads((NATIVE / "patch_manifest.json").read_text())
            for name in manifest["groups"]["softhier"]["files"]:
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / "SoftHier" / name, target)
            apply_native.apply("softhier", stage)
            template = (NATIVE / "tests/clock_queue_fixture.cpp").read_text()
            traces = []
            for name, source in (("original", ROOT / "SoftHier"), ("cached", stage)):
                cpp = (source / "engine/engine/src/clock/clock_engine.cpp").read_text()
                header = (source / "engine/engine/include/vp/clock/clock_engine.hpp").read_text()
                functions = [method(cpp, signature) for signature in (
                    "void vp::ClockEngine::update()", "vp::ClockEvent *vp::ClockEngine::enqueue(",
                    "void vp::ClockEngine::cancel(", "int64_t vp::ClockEngine::exec()",
                    "vp::ClockEvent *vp::ClockEngine::reenqueue(", "void vp::ClockEngine::reset(")]
                functions.append(method(header, "void event_del(").replace(
                    "void event_del(Block *comp, ClockEvent *event)",
                    "void vp::ClockEngine::event_del(vp::Block *comp, vp::ClockEvent *event)"))
                path = folder / (name + ".cpp")
                path.write_text(template.replace("// ACTUAL_ENGINE_METHODS", "\n\n".join(functions)))
                binary = path.with_suffix("")
                compiled = subprocess.run(["g++", "-std=c++17", "-O1", "-g", "-fsanitize=address,undefined",
                                           "-fno-omit-frame-pointer", str(path), "-o", str(binary)],
                                          capture_output=True, text=True)
                self.assertEqual(compiled.returncode, 0, compiled.stderr)
                result = subprocess.run([str(binary)], capture_output=True,
                    preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)))
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                traces.append(result.stdout)
            self.assertEqual(traces[0], traces[1], "queue contents, execution order, cycles, or wake-ups changed")


if __name__ == "__main__":
    unittest.main()
