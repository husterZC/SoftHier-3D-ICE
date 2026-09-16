"""Area-preserving rectangle helpers shared by SoftHier placement rules.

All lengths are in micrometers and all areas are in square micrometers.
Power paths and coefficients are deliberately not part of this module.
"""

import math

RESIDUAL_AREA_FRACTION = 0.05


def rectangle(x, y, width, height):
    return {"type": "comp", "shape": [width, height], "offset": [x, y], "subs": {}}


def with_residual(inventory):
    """Keep the original reserve: 5% of everything except RedMulE and TCDM."""
    items = [dict(item) for item in inventory]
    names = [item["name"] for item in items]
    if len(set(names)) != len(names) or "others" in names:
        raise ValueError("floorplan inventory must have unique names; 'others' is reserved")
    if not {"redmule", "tcdm"}.issubset(names):
        raise ValueError("floorplan requires redmule and tcdm")
    for item in items:
        area = item["area_um2"]
        if not math.isfinite(area) or area <= 0:
            raise ValueError("floorplan area must be finite and positive: " + item["name"])
    support_area = sum(item["area_um2"] for item in items if item["name"] not in ("redmule", "tcdm"))
    if support_area <= 0:
        raise ValueError("floorplan requires components beyond redmule and tcdm")
    items.append({"name": "others", "kind": "unmodeled", "area_um2": support_area * RESIDUAL_AREA_FRACTION})
    return items


def partition_regions(items, x, y, width, height):
    """Original binary partition, retained exactly for default-layout stability."""
    if len(items) == 1:
        return {items[0]["name"]: rectangle(x, y, width, height)}
    total = sum(item["area_um2"] for item in items)
    split = min(range(1, len(items)), key=lambda i: abs(sum(item["area_um2"] for item in items[:i]) / total - 0.5))
    fraction = sum(item["area_um2"] for item in items[:split]) / total
    if width >= height:
        first = partition_regions(items[:split], x, y, width * fraction, height)
        second = partition_regions(items[split:], x + width * fraction, y, width * (1 - fraction), height)
    else:
        first = partition_regions(items[:split], x, y, width, height * fraction)
        second = partition_regions(items[split:], x, y + height * fraction, width, height * (1 - fraction))
    return dict(first, **second)


def split_columns(items, x, y, height):
    """Left-to-right rectangles with a shared height and unchanged areas."""
    regions = {}
    for item in items:
        width = item["area_um2"] / height
        regions[item["name"]] = rectangle(x, y, width, height)
        x += width
    return regions


def validate_layout(inventory, regions, shape):
    """Reject missing domains, altered areas, overlaps, holes or invalid bounds."""
    if len(shape) != 2 or any(not math.isfinite(v) or v <= 0 for v in shape):
        raise ValueError("invalid cluster shape")
    expected = {item["name"]: item["area_um2"] for item in inventory}
    if set(regions) != set(expected):
        raise ValueError("floorplan must contain every inventory region exactly once")
    total_area = sum(expected.values())
    if not math.isclose(shape[0] * shape[1], total_area, rel_tol=1e-9, abs_tol=1e-7):
        raise ValueError("cluster area differs from total component area")
    coordinate_tolerance = max(shape) * 1e-10
    placed = []
    for name, region in regions.items():
        x, y = region["offset"]
        width, height = region["shape"]
        if any(not math.isfinite(v) for v in (x, y, width, height)) or min(width, height) <= 0:
            raise ValueError("invalid rectangle for " + name)
        if region["type"] != "comp" or region["subs"]:
            raise ValueError("floorplan regions must be component leaves: " + name)
        if min(x, y) < -coordinate_tolerance or x + width > shape[0] + coordinate_tolerance or y + height > shape[1] + coordinate_tolerance:
            raise ValueError("region outside cluster: " + name)
        if not math.isclose(width * height, expected[name], rel_tol=1e-9, abs_tol=1e-7):
            raise ValueError("floorplan changed component area: " + name)
        for other_name, ox, oy, ow, oh in placed:
            overlap = max(0., min(x + width, ox + ow) - max(x, ox)) * max(0., min(y + height, oy + oh) - max(y, oy))
            if overlap > max(1e-7, total_area * 1e-10):
                raise ValueError("overlapping floorplan regions: " + name + ", " + other_name)
        placed.append((name, x, y, width, height))
