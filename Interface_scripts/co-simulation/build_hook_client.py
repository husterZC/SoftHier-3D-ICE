"""Build the executable hook shim in an ignored, content-addressed cache."""
import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(__file__).with_name("native") / "power_hook_client.c"
HEADER = ROOT / "Interface_scripts/providers/softhier/native/closed_loop_timing.h"


def build():
    compiler = shlex.split(os.environ.get("CC", "cc"))
    version = subprocess.check_output([*compiler, "--version"])
    key = hashlib.sha256(SOURCE.read_bytes() + HEADER.read_bytes() + version + repr(compiler).encode()).hexdigest()
    directory = ROOT / "runs/.coupling_tools" / key
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "power_hook_client"
    if not binary.exists():
        fd, temporary = tempfile.mkstemp(dir=directory)
        os.close(fd)
        try:
            subprocess.run([*compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                            str(SOURCE), "-lm", "-o", temporary], check=True)
            Path(temporary).chmod(0o755)
            os.replace(temporary, binary)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return binary


if __name__ == "__main__":
    print(build())
