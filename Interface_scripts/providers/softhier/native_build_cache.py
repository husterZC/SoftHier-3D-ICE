#!/usr/bin/env python3
"""Identity and artifact checks for the provider's installed DRAMSys library."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "native"))
from apply_native import fingerprint


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def identity(base_commit, upstream_patch, systemc_home, systemc_version):
    return {"schema_version": 1, "base_commit": base_commit,
            "upstream_patch_sha256": digest(upstream_patch), "native_bundle_sha256": fingerprint(),
            "systemc_version": systemc_version, "systemc_home": str(Path(systemc_home).resolve()),
            "systemc_library_sha256": digest(Path(systemc_home) / "lib64/libsystemc.so")}


def cache_key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def stamp_or_check(value, home, check=False):
    home = Path(home)
    marker = home / "native_build.json"
    document = {"identity": value, "library_sha256": digest(home / "libDRAMSys_Simulator.so")}
    if check:
        if json.loads(marker.read_text()) != document:
            raise ValueError("installed DRAMSys base/patches/SystemC/library differ from the build stamp")
    else:
        temporary = marker.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(document, indent=2) + "\n")
        temporary.replace(marker)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("key", "stamp", "check"))
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("--upstream-patch", required=True, type=Path)
    parser.add_argument("--systemc-home", required=True, type=Path)
    parser.add_argument("--systemc-version", required=True)
    parser.add_argument("--home", type=Path)
    args = parser.parse_args()
    value = identity(args.base_commit, args.upstream_patch, args.systemc_home, args.systemc_version)
    if args.action == "key":
        print(cache_key(value))
    else:
        if args.home is None:
            parser.error("stamp/check requires --home")
        stamp_or_check(value, args.home, args.action == "check")


if __name__ == "__main__":
    main()
