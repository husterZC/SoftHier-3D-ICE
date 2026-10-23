#!/usr/bin/env python3
"""Stage pinned GVSoC sources without modifying the upstream checkouts."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE / "native"))
from apply_native import apply


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root)] + list(args)).decode().strip()


def copy_tracked(source, destination):
    destination.mkdir(parents=True, exist_ok=True)
    names = subprocess.check_output(["git", "-C", str(source), "ls-files", "-z"]).decode().split("\0")
    for name in filter(None, names):
        src = source / name
        dst = destination / name
        src.resolve().relative_to(REPO)
        if src.is_dir():  # Gitlink: unmodified modules can be shared.
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))


def source_identity(source):
    source = source.resolve()
    source.relative_to(REPO)
    bases = {}
    pending = [source]
    while pending:
        module = pending.pop()
        module.resolve().relative_to(REPO)
        # An uninitialized gitlink otherwise makes `git -C` silently operate
        # on its parent repository. Every module is consumed by the shared
        # source stage, including gvrun/config_tree and nested submodules.
        if not (module / ".git").exists():
            raise ValueError("uninitialized source module: {}".format(module))
        top = Path(git(module, "rev-parse", "--show-toplevel")).resolve()
        if top != module.resolve():
            raise ValueError("source module is not its own Git checkout: {}".format(module))
        name = str(module.relative_to(source))
        bases[name] = git(module, "rev-parse", "HEAD")
        # Never silently build a snapshot that omits local tracked edits.
        if git(module, "diff", "HEAD", "--name-only", "--ignore-submodules=all"):
            raise ValueError("tracked source edits in {}; use a clean pinned checkout".format(module))
        entries = git(module, "ls-files", "--stage", "-z").split("\0")
        for entry in filter(None, entries):
            metadata, path = entry.split("\t", 1)
            if metadata.split()[0] == "160000":
                child = module / path
                child.resolve().relative_to(REPO)
                pending.append(child)
    specification = json.loads((HERE / "native/patch_manifest.json").read_text())
    # DRAMSys headers/reporting do not change GVSoC sources. Keep their cache
    # identities independent while the provider checks both installed builds.
    patch_identity = {"specification": specification["groups"]["softhier"],
                      "patch_sha256": file_digest(HERE / "native/softhier.patch"),
                      "applicator_sha256": file_digest(HERE / "native/apply_native.py"),
                      "timing_header_sha256": file_digest(HERE / "native/closed_loop_timing.h")}
    return {"bases": bases, "softhier_patch": patch_identity,
            "stager_sha256": file_digest(Path(__file__))}


def file_digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def stage(source, workdir):
    source, workdir = source.resolve(), workdir.resolve()
    source.relative_to(REPO)
    workdir.relative_to(REPO)
    identity = source_identity(source)
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    destination = workdir / "native_sources" / key
    marker = destination / "stage.json"
    if marker.exists():
        if json.loads(marker.read_text()) != identity:
            raise ValueError("native stage identity mismatch: {}".format(destination))
        apply("softhier", destination)
        return destination
    if destination.exists():
        raise ValueError("incomplete native stage; remove this provider-generated directory before retrying: {}".format(destination))
    copy_tracked(source, destination)
    for module in ("core", "engine", "pulp"):
        (destination / module).unlink()
        copy_tracked(source / module, destination / module)
    for name in ("soft_hier_sdk", "third_party"):
        if (source / name).exists() and not (destination / name).exists():
            (destination / name).symlink_to(source / name)
    apply("softhier", destination)
    marker.write_text(json.dumps(identity, indent=2) + "\n")
    return destination


BUILD_ARTIFACTS = (
    "bin/gvsoc", "lib/libpulpvp.so", "models/memory/dramsys.so",
    "bin/gvsoc_launcher_profile_sc", "lib/libpulpvp-profile.so", "models/profile/memory/dramsys.so",
)


def installed_artifacts(workdir):
    install = workdir.resolve() / "install"
    install.relative_to(REPO)
    result = {name: file_digest(install / name) for name in BUILD_ARTIFACTS}
    # The normal model intentionally compiles out power accounting. Co-simulation
    # selects the profile/SystemC launcher and its matching engine and models.
    symbols = subprocess.check_output(["nm", "-D", "-C", str(install / "lib/libpulpvp-profile.so")]).decode()
    if " T vp::BlockPower::get_external_energy(" not in symbols:
        raise ValueError("installed GVSoC engine lacks the external-energy callback")
    if b"dram_get_core_energy_j" not in (install / "models/profile/memory/dramsys.so").read_bytes():
        raise ValueError("installed DRAM wrapper lacks the cumulative-energy ABI")
    return result


def systemc_identity(home, version):
    home = Path(home).resolve()
    home.relative_to(REPO)
    library = (home / "lib64/libsystemc.so").resolve()
    library.relative_to(REPO)
    if not version:
        raise ValueError("SystemC version is required")
    return {"home": str(home), "version": version,
            "library_sha256": file_digest(library)}


def build_stamp(source, workdir, target, systemc_home, systemc_version, staged_source=None, check=False):
    workdir = workdir.resolve()
    workdir.relative_to(REPO)
    marker = workdir / "install/native_build.json"
    sources = source_identity(source)
    identity = {"sources": sources, "systemc": systemc_identity(systemc_home, systemc_version)}
    if check:
        document = json.loads(marker.read_text())
        if document.get("identity") != identity or document.get("target") != target:
            raise ValueError("installed GVSoC sources/patches/target differ or SystemC changed; rebuild hardware")
        staged_source = Path(document["staged_source"]).resolve()
    else:
        if staged_source is None:
            raise ValueError("stamp requires --stage")
        staged_source = staged_source.resolve()
    staged_source.relative_to(workdir.resolve())
    if json.loads((staged_source / "stage.json").read_text()) != sources:
        raise ValueError("native source stage identity does not match the requested build")
    artifacts = installed_artifacts(workdir)
    if check:
        if document.get("artifacts_sha256") != artifacts:
            raise ValueError("installed GVSoC artifacts differ from their build stamp")
    else:
        # Verify every native patch postcondition before claiming these sources.
        apply("softhier", staged_source)
        document = {"schema_version": 2, "identity": identity, "target": target,
                    "staged_source": str(staged_source), "artifacts_sha256": artifacts}
        temporary = marker.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(document, indent=2) + "\n")
        temporary.replace(marker)
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--action", choices=("stage", "stamp", "check"), default="stage")
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--target")
    parser.add_argument("--systemc-home", type=Path)
    parser.add_argument("--systemc-version")
    args = parser.parse_args()
    if args.action == "stage":
        print(stage(args.source, args.workdir))
    else:
        if not args.target:
            parser.error("stamp/check requires --target")
        if not args.systemc_home or not args.systemc_version:
            parser.error("stamp/check requires --systemc-home and --systemc-version")
        document = build_stamp(args.source, args.workdir, args.target,
                               args.systemc_home, args.systemc_version, args.stage, args.action == "check")
        print(document["staged_source"])


if __name__ == "__main__":
    main()
