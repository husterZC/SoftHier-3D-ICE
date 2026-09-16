"""Square cluster, bottom compute/memory, middle PE/Spatz pairs, top support."""

import math
import re

from .common import rectangle, split_columns

DESCRIPTION = "area-estimated square cluster with RedMulE/TCDM, PE/Spatz and support horizontal bands"


def core_index(item):
    match = re.fullmatch(r"pe(\d+)", item["name"])
    if match is None:
        raise ValueError("square_bands expects scalar core names pe<N>")
    return int(match[1])


def support_priority(item):
    if item["name"] == "instr_mem":
        return 0
    if item["name"] == "stack_mem":
        return 1
    return {"idma": 2, "floonoc": 3}.get(item.get("kind"), 4)


def build(inventory):
    by_name = {item["name"]: item for item in inventory}
    cores = sorted((item for item in inventory if item.get("kind") == "core"), key=core_index)
    if not cores:
        raise ValueError("square_bands requires at least one scalar core")
    vectors = {item["name"]: item for item in inventory if item.get("kind") == "spatz"}
    pairs = [(core, vectors.get("spatz" + str(core_index(core)))) for core in cores]
    paired_vectors = {vector["name"] for _, vector in pairs if vector is not None}
    if paired_vectors != set(vectors):
        raise ValueError("each Spatz must have a matching pe<N> scalar core")
    band_a = [by_name["redmule"], by_name["tcdm"]]
    used = {item["name"] for item in band_a + cores} | paired_vectors
    band_c = sorted((item for item in inventory if item["name"] not in used), key=support_priority)

    side = math.sqrt(sum(item["area_um2"] for item in inventory))
    height_a = sum(item["area_um2"] for item in band_a) / side
    height_b = sum(core["area_um2"] + (vector["area_um2"] if vector else 0.) for core, vector in pairs) / side
    height_c = sum(item["area_um2"] for item in band_c) / side
    regions = split_columns(band_a, 0., 0., height_a)
    x = 0.
    for core, vector in pairs:
        pair_area = core["area_um2"] + (vector["area_um2"] if vector else 0.)
        width = pair_area / height_b
        vector_height = vector["area_um2"] / width if vector else 0.
        if vector:
            regions[vector["name"]] = rectangle(x, height_a, width, vector_height)
        regions[core["name"]] = rectangle(x, height_a + vector_height, width, core["area_um2"] / width)
        x += width
    regions.update(split_columns(band_c, 0., height_a + height_b, height_c))
    return regions, [side, side]
