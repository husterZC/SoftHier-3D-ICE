#!/usr/bin/env python3
"""Check the installed SystemC headers and library against a requested release."""
import argparse
from pathlib import Path
import re
import subprocess

REPO = Path(__file__).resolve().parents[3]


def validate(home, version):
    home = Path(home).resolve()
    home.relative_to(REPO)
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("SOFTHIER_SYSTEMC_VERSION must be a release such as 3.0.1")
    header = home / "include/sysc/kernel/sc_ver.h"
    library = home / "lib64/libsystemc.so"
    for path in (home / "include/systemc.h", header, library):
        path.resolve().relative_to(REPO)
        if not path.is_file():
            raise ValueError(f"missing SystemC installation file: {path}")
    text = header.read_text()
    parts = []
    for name in ("MAJOR", "MINOR", "PATCH"):
        matches = re.findall(r"^\s*#\s*define\s+SC_VERSION_" + name + r"\s+(\d+)\s*$", text, re.M)
        if len(matches) != 1:
            raise ValueError(f"cannot determine SystemC {name.lower()} version from {header}")
        parts.append(str(int(matches[0])))
    installed = ".".join(parts)
    if installed != version:
        raise ValueError(f"installed SystemC headers are {installed}, requested {version}")
    symbols = subprocess.check_output(["nm", "-D", "-C", str(library)], text=True)
    if "sc_core::sc_api_version_" + "_".join(parts) + "_" not in symbols:
        raise ValueError(f"SystemC library does not export the {version} header ABI")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    try:
        validate(args.home, args.version)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"SystemC installation check failed: {exc}\n")


if __name__ == "__main__":
    main()
