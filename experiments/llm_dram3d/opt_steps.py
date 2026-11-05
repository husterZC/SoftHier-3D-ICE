"""User-facing names for selected cumulative SDK decode optimization steps."""

import argparse

OPT_STEPS = {
    0: ("OM", "Owner Mapping"),
    9: ("DO", "DMA Overlap"),
    13: ("DW", "Distributing Workers"),
    16: ("SW", "Striped Weights"),
    17: ("LC", "Local Channels"),
}


def parse_opt_step(value):
    """Accept existing numeric steps and case-insensitive aliases."""
    text = str(value).strip()
    aliases = {alias: step for step, (alias, _) in OPT_STEPS.items()}
    if text.upper() in aliases:
        return aliases[text.upper()]
    try:
        step = int(text)
        if 0 <= step <= 26:
            return step
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("OPT step must be 0..26 or OM, DO, DW, SW, LC")


def opt_metadata(step):
    alias, name = OPT_STEPS.get(step, (None, None))
    return dict(opt_step=step, opt_alias=alias, opt_name=name)


def opt_label(step):
    alias, name = OPT_STEPS.get(step, (None, None))
    return f"OPT {step} ({alias}: {name})" if alias else f"OPT {step}"
