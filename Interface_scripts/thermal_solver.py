"""CPU solver settings shared by stack generation and thermal replay."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re

SOLVERS = ("cpu_direct_lu", "cpu_amgcl_cg_amg")


def solver_section(text):
    matches = list(re.finditer(r"(?m)^\s*solver\s*:\s*", text))
    if len(matches) != 1:
        raise ValueError("missing or ambiguous solver section")
    start = matches[0].end()
    end = re.search(r"(?m)^\s*output\s*:", text[start:])
    return start, start + end.start() if end else len(text)


def settings(text):
    start, end = solver_section(text)
    block = text[start:end]

    def value(name, default):
        matches = re.findall(rf"\b{name}\s+([^;\s]+)\s*;", block)
        if len(matches) > 1:
            raise ValueError(f"ambiguous solver {name}")
        return matches[0] if matches else default

    solver = value("type", "cpu_direct_lu")
    return dict(solver_type=solver, solver_threads=int(value("numofcores", "1")),
                solver_tolerance=float(value("tolerance", "1e-4")) if solver != "cpu_direct_lu" else None)


def configure(text, solver_type=None, solver_threads=None, solver_tolerance=None):
    """Override only solver controls, leaving all thermal physics intact."""
    if solver_type is not None and solver_type not in SOLVERS:
        raise ValueError(f"unsupported CPU solver: {solver_type}")
    if solver_threads is not None and (type(solver_threads) is not int or solver_threads < 1):
        raise ValueError("solver thread count must be a positive integer")
    if solver_tolerance is not None and (not math.isfinite(solver_tolerance) or solver_tolerance <= 0):
        raise ValueError("solver tolerance must be positive and finite")
    current = settings(text)
    effective = solver_type or current["solver_type"]
    if solver_tolerance is not None and effective != "cpu_amgcl_cg_amg":
        raise ValueError("solver tolerance is supported only for cpu_amgcl_cg_amg")
    if solver_type == "cpu_amgcl_cg_amg" and solver_tolerance is None:
        solver_tolerance = 1e-6
    start, end = solver_section(text)
    block = text[start:end]
    # The native grammar requires type, numofcores, tolerance in this order.
    selected_type = solver_type or re.search(r"\btype\s+([^;\s]+)\s*;", block)
    if hasattr(selected_type, "group"):
        selected_type = selected_type[1]
    threads = solver_threads if solver_threads is not None else current["solver_threads"]
    tolerance = solver_tolerance if solver_tolerance is not None else current["solver_tolerance"]
    if effective == "cpu_direct_lu":
        tolerance = None
    if all(option is None for option in (solver_type, solver_threads, solver_tolerance)):
        return text
    block = re.sub(r"\b(?:type|numofcores|tolerance)\s+[^;]+;\s*", "", block).rstrip()
    controls = (f"   type {selected_type} ;\n" if selected_type else "")
    controls += f"   numofcores {threads} ;\n"
    if tolerance is not None:
        controls += f"   tolerance {tolerance:.12g} ;\n"
    return text[:start] + block + "\n" + controls + "\n" + text[end:]


def environment(threads):
    return dict(os.environ, OMP_NUM_THREADS=str(threads), OPENBLAS_NUM_THREADS="1",
                TDICE_OPENBLAS_NUM_THREADS="1")


def describe(stack, server):
    server = Path(server).resolve()
    return dict(settings(Path(stack).read_text()), server=str(server),
                server_sha256=hashlib.sha256(server.read_bytes()).hexdigest(), blas_threads=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("describe", "run"))
    parser.add_argument("--stack", type=Path, required=True)
    parser.add_argument("--server", type=Path, required=True)
    args, command = parser.parse_known_args()
    if args.command == "describe":
        if command:
            parser.error("unexpected arguments")
        print(json.dumps(describe(args.stack, args.server), indent=2))
    else:
        if command[:1] == ["--"]:
            command = command[1:]
        config = settings(args.stack.read_text())
        server = str(args.server.resolve())
        os.execve(server, [server, *command], environment(config["solver_threads"]))


if __name__ == "__main__":
    main()
