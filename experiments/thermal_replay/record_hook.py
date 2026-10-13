#!/usr/bin/env python3
"""Constant-temperature GVSoC hook; the engine writes the activity JSONL."""

import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("init", "update", "final"), required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    protocol = {"name": "gvsoc-power-hook", "version": 1}
    if request["protocol"] != protocol or request["phase"] != args.phase:
        raise ValueError("unexpected power-hook protocol or phase")
    config = json.loads(args.config.read_text())
    # Only init needs updates. With no thermal feedback the initial temperature
    # and voltage remain unchanged throughout capture, and constant-profile
    # leakage gives the reference for the actual modeled (including padded) SRAM.
    updates = config["temperatures"] if args.phase == "init" else []
    response = {"protocol": protocol, "phase": args.phase,
                "temperature_unit": "celsius", "temperatures": updates}
    temporary = args.response.with_suffix(f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(response) + "\n")
    temporary.replace(args.response)


if __name__ == "__main__":
    main()
