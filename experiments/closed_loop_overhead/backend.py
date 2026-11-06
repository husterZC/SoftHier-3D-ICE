"""Explicit thermal-backend selection and evidence shared by overhead studies."""

import json
import math
from pathlib import Path

from Interface_scripts import thermal_solver

ROOT = Path(__file__).resolve().parents[2]
LU = "cpu_direct_lu"
CG = "cpu_amgcl_cg_amg"


def add_arguments(parser, positive_int, positive_float):
    parser.add_argument("--dice-dir", type=Path, default=ROOT / "3D-ICE",
                        help="Repository-local 3D-ICE source directory")
    parser.add_argument("--dice-build-target", choices=("all", "cpu"), default="all")
    parser.add_argument("--solver-type", choices=thermal_solver.SOLVERS, default=LU)
    parser.add_argument("--solver-threads", type=positive_int, default=1)
    parser.add_argument("--solver-tolerance", type=positive_float,
                        help="CG absolute residual tolerance; default 1e-6")


def selection(directory=ROOT / "3D-ICE", target="all", solver=LU, threads=1, tolerance=None):
    directory = Path(directory).resolve()
    try:
        directory.relative_to(ROOT)
    except ValueError as error:
        raise ValueError("overhead studies require --dice-dir inside the repository for source snapshots") from error
    if not (directory / "Makefile").is_file():
        raise ValueError(f"missing 3D-ICE source directory: {directory}")
    modern = (directory / "sources/cpu_amgcl_solver.cpp").is_file()
    if target not in ("all", "cpu") or (modern and target != "cpu") or (target == "cpu" and not modern):
        raise ValueError("use --dice-build-target cpu for the new backend, or all for the original backend")
    if solver not in thermal_solver.SOLVERS or (solver == CG and target != "cpu"):
        raise ValueError("AMGCL CG requires the new CPU backend")
    if type(threads) is not int or threads < 1:
        raise ValueError("solver thread count must be positive")
    if tolerance is not None and (not math.isfinite(tolerance) or tolerance <= 0 or solver != CG):
        raise ValueError("a positive finite tolerance is supported only for AMGCL CG")
    suffix = "-cpu" if target == "cpu" else ""
    return dict(dice_dir=str(directory), build_target=target,
                server=str(directory / "bin" / ("3D-ICE-Server" + suffix)),
                client=str(directory / "bin" / ("3D-ICE-Client" + suffix)),
                solver_type=solver, solver_threads=threads,
                solver_tolerance=(1e-6 if tolerance is None else tolerance) if solver == CG else None,
                blas_threads=1)


def from_args(args):
    return selection(args.dice_dir, args.dice_build_target, args.solver_type,
                     args.solver_threads, args.solver_tolerance)


def from_settings(settings):
    """Legacy studies had an implicit original-backend, one-thread LU selection.

    Do not inspect current binaries here: offline analysis uses saved evidence.
    """
    if "thermal_backend" in settings:
        return settings["thermal_backend"]
    return dict(dice_dir=str(ROOT / "3D-ICE"), build_target="all",
                server=str(ROOT / "3D-ICE/bin/3D-ICE-Server"),
                client=str(ROOT / "3D-ICE/bin/3D-ICE-Client"), solver_type=LU,
                solver_threads=settings.get("thermal_threads", 1), solver_tolerance=None, blas_threads=1)


def make_arguments(config):
    return [f"DICE_DIR={config['dice_dir']}", f"DICE_BUILD_TARGET={config['build_target']}",
            # The original parser has no solver type keyword.
            f"ICE_SOLVER_TYPE={config['solver_type'] if config['build_target'] == 'cpu' else ''}",
            f"ICE_SOLVER_THREADS={config['solver_threads']}",
            f"ICE_SOLVER_TOLERANCE={config['solver_tolerance'] if config['solver_tolerance'] is not None else ''}"]


def replay_arguments(config):
    args = ["--server", config["server"], "--solver-threads", str(config["solver_threads"])]
    if config["build_target"] == "cpu":
        args += ["--solver-type", config["solver_type"]]
    if config["solver_tolerance"] is not None:
        args += ["--solver-tolerance", str(config["solver_tolerance"])]
    return args


def verify_metadata(actual, config, checksum=None):
    for key in ("server", "solver_type", "solver_threads", "solver_tolerance", "blas_threads"):
        if key not in actual or actual[key] != config[key]:
            raise ValueError(f"effective thermal backend differs in {key}")
    if checksum is not None and actual.get("server_sha256") != checksum:
        raise ValueError("effective thermal server hash differs from the study")


def verify_run(directory, config, checksum):
    directory = Path(directory)
    actual = thermal_solver.describe(directory / "generated/3dice/ice.stk", config["server"])
    verify_metadata(actual, config, checksum)
    if config["build_target"] == "cpu":
        launched = json.loads((directory / "generated/thermal_solver.json").read_text())
        verify_metadata(launched, config, checksum)
    return actual


def setup_events(events, solver, start, roi_start):
    """Require exactly one algorithm-appropriate setup, entirely before ROI."""
    expected = {LU: "factorization", CG: "preconditioner_setup"}.get(solver)
    if expected is None:
        raise ValueError(f"unsupported thermal solver: {solver}")
    setups = [e for e in events if e["phase"] in ("factorization", "preconditioner_setup")]
    if (len(setups) != 1 or setups[0]["phase"] != expected
            or not start <= setups[0]["start_ns"] <= setups[0]["end_ns"] <= roi_start):
        raise ValueError(f"expected one native {expected} before workload/replay execution")
    return setups
