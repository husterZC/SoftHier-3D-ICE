#!/usr/bin/env python3
"""Apply provider-owned native energy patches to exact staged source revisions."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
HEADERS = ("softhier_hbm2_core.hpp", "softhier_hbm2_power.hpp")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint() -> str:
    value = hashlib.sha256()
    for name in ("apply_native.py", "patch_manifest.json", "softhier.patch", "dramsys.patch", *HEADERS):
        value.update(name.encode())
        value.update((HERE / name).read_bytes())
    return value.hexdigest()


def apply(group: str, root: Path) -> dict:
    root = root.resolve()
    # User restriction: even symlink targets and subprocess cwd stay in this repo.
    root.relative_to(REPO)
    manifest = json.loads((HERE / "patch_manifest.json").read_text())
    patch = HERE / f"{group}.patch"
    specification = manifest["groups"][group]
    if digest(patch) != specification["patch_sha256"]:
        raise ValueError(f"{group}: patch hash does not match manifest")
    states = set()
    for relative, expected in specification["files"].items():
        path = (root / relative).resolve()
        path.relative_to(root)
        actual = digest(path)
        if actual == expected["before"]:
            states.add("before")
        elif actual == expected["after"]:
            states.add("after")
        else:
            raise ValueError(f"{path}: unsupported source contents; use the pinned clean/staged source")
    if states == {"before"}:
        # --directory is relative to repository root. git apply from a nested
        # non-repository cwd can silently skip every patch, so never use that.
        directory = str(root.relative_to(REPO))
        args = ["git", "apply", f"--directory={directory}", str(patch)]
        subprocess.run(args[:2] + ["--check"] + args[2:], cwd=REPO, check=True)
        subprocess.run(args, cwd=REPO, check=True)
    elif states != {"after"}:
        raise ValueError(f"{group}: partially patched source; restage before continuing")
    for relative, expected in specification["files"].items():
        if digest(root / relative) != expected["after"]:
            raise ValueError(f"{relative}: patch postcondition failed")
    if group == "dramsys":
        for name in HEADERS:
            shutil.copyfile(HERE / name, root / "src/DRAMSys/simulation" / name)
    result = {"bundle_sha256": fingerprint(), "bases": manifest["bases"], "group": group,
              "source": str(root), "patch_sha256": digest(patch)}
    (root / ".softhier_native_energy.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--softhier", type=Path)
    parser.add_argument("--dramsys", type=Path, help="DRAMSys with the pinned SoftHier shared-library patch already applied")
    parser.add_argument("--fingerprint", action="store_true")
    args = parser.parse_args()
    if args.fingerprint:
        print(fingerprint())
    for group in ("softhier", "dramsys"):
        root = getattr(args, group)
        if root is not None:
            print(json.dumps(apply(group, root)))


if __name__ == "__main__":
    main()
