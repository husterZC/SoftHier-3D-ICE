"""Command-line entry point: python -m experiments.thermal_replay --help."""

import argparse
import json
from pathlib import Path
import signal
import sys

from .activity import ROOT
from .record import capture, record
from .replay import run
from .validate import validate


def interrupted(*_):
    raise InterruptedError("terminated")


def main():
    parser = argparse.ArgumentParser(description="Record a block of layers, then replay power and thermal simulation.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("capture", "record"):
        command = commands.add_parser(name, help="run GVSoC without 3D-ICE" if name == "capture" else "import a saved hook trace")
        command.add_argument("--source-run" if name == "capture" else "--run", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--layer-start-ps", type=int)
        command.add_argument("--layer-end-ps", type=int)
        command.add_argument("--layers-per-cycle", type=int, default=1,
                             help="layers in the measured ROI (default: 1; use 10 for a repeat=10 block)")
        command.add_argument("--heat-transfer-coefficient", type=float, metavar="W_PER_M2_K",
                             help="override the recording's top heat sink in W/(m² K); default: archived value")
        if name == "capture":
            command.add_argument("--provider", type=Path, default=ROOT / "Interface_scripts/providers/softhier/provider.sh")
        else:
            command.add_argument("--whole-trace", action="store_true", help="transport validation only; not repeatable")
    replay = commands.add_parser("replay", help="keep one native thermal solver alive across all layers")
    replay.add_argument("--recording", type=Path, required=True)
    replay.add_argument("--output", type=Path, required=True)
    replay.add_argument("--server", type=Path, default=ROOT / "3D-ICE/bin/3D-ICE-Server")
    replay.add_argument("--profile", choices=("constant", "temperature_aware", "recorded"), default="temperature_aware")
    mode = replay.add_mutually_exclusive_group()
    mode.add_argument("--cycles", type=int, help="fixed number of recorded blocks, subject to the time cap")
    mode.add_argument("--single-pass", action="store_true", help="replay every original window once, including startup/reporting")
    replay.add_argument("--max-seconds", type=float, default=1., help="simulated time cap (default: 1 s)")
    replay.add_argument("--allow-model-clamp", action="store_true",
                        help="continue above 125 C using GVSoC's endpoint leakage clamp")
    replay.add_argument("--thermal-step", type=float, metavar="SECONDS",
                        help="thermal integration step in seconds (default: equal to the recorded power slot)")
    replay.add_argument("--dense-maps", action="store_true", help="also write dense Tmap at every power interval")
    replay.add_argument("--timeout", type=float, default=180., help="native server startup/socket timeout")
    replay.add_argument("--factorization-threads", type=int, default=1,
                        help="SuperLU factorization threads (default: 1; see native stability note)")
    check = commands.add_parser("validate", help="compare replay/leakage against saved co-simulation results")
    check.add_argument("--output", type=Path, required=True)
    for option in ("constant-run", "aware-run", "replay-output", "reference-run"):
        check.add_argument("--" + option, type=Path)
    check.add_argument("--single-pass", action="store_true", help="also compare every temperature row")
    args = vars(parser.parse_args())
    command = args.pop("command")
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if command == "capture":
            args["start"], args["end"] = args.pop("layer_start_ps"), args.pop("layer_end_ps")
            result = capture(**args)
        elif command == "record":
            args["start"], args["end"] = args.pop("layer_start_ps"), args.pop("layer_end_ps")
            result = record(**args)
        elif command == "validate":
            result = validate(**args)
        else:
            result = run(**args)
        print(json.dumps({k: v for k, v in result.items() if k not in ("component_paths", "sha256")}, indent=2))
        return 2 if result.get("status") == "model_limit" or result.get("passed") is False else 0
    except (KeyboardInterrupt, InterruptedError):
        print("Thermal replay interrupted; completed history and status were retained.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"thermal_replay: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
