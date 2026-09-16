"""Original layout: square RedMulE, right-hand TCDM strip, support above."""

import math

from .common import partition_regions, rectangle

DESCRIPTION = "area-estimated cluster layout with binary-partitioned support region"


def build(inventory):
    by_name = {item["name"]: item for item in inventory}
    redmule_dimension = math.sqrt(by_name["redmule"]["area_um2"])
    tcdm_area = by_name["tcdm"]["area_um2"]
    support = [item for item in inventory if item["name"] not in ("redmule", "tcdm")]
    support_height = sum(item["area_um2"] for item in support) / redmule_dimension
    cluster_height = redmule_dimension + support_height
    tcdm_width = tcdm_area / cluster_height
    components = {
        "redmule": rectangle(0., 0., redmule_dimension, redmule_dimension),
        "tcdm": rectangle(redmule_dimension, 0., tcdm_width, cluster_height),
    }
    components.update(partition_regions(support, 0., redmule_dimension, redmule_dimension, support_height))
    return components, [redmule_dimension + tcdm_width, cluster_height]
