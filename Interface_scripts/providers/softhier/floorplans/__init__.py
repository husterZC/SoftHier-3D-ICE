"""Selectable placement rules; component areas and power mappings stay fixed."""

from . import redmule_strip, square_bands
from .common import RESIDUAL_AREA_FRACTION, validate_layout, with_residual

DEFAULT_RULE = "redmule_strip"
RULES = {"redmule_strip": redmule_strip, "square_bands": square_bands}


def get_rule(name):
    if name not in RULES:
        raise ValueError("unknown SoftHier floorplan {!r}; choose {}".format(name, ", ".join(RULES)))
    return RULES[name]


def build_cluster(inventory, rule=DEFAULT_RULE):
    placement = get_rule(rule)
    items = with_residual(inventory)
    regions, shape = placement.build(items)
    validate_layout(items, regions, shape)
    return regions, shape
