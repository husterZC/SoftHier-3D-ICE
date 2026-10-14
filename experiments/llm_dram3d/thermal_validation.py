"""Physical checks shared by memory-on-logic studies and small integration cases."""

import math
from pathlib import Path
import re


def thermal_bounds(contract, stack_text):
    initial = re.findall(r"\binitial\s+temperature\s+([^\s;]+)\s*;", stack_text)
    cooling = re.findall(r"\b(?:top|bottom)\s+heat\s+sink\s*:\s*heat\s+transfer\s+coefficient\s+[^;]+;\s*temperature\s+([^\s;]+)\s*;", stack_text)
    if len(initial) != 1 or not cooling:
        raise ValueError("cannot determine the stack's initial and cooling temperatures")
    initial_k, cooling_k = float(initial[0]), [float(value) for value in cooling]
    if not all(math.isfinite(value) and value > 0 for value in [initial_k] + cooling_k):
        raise ValueError("invalid stack thermal boundary")
    if not math.isclose(initial_k, float(contract["thermal_feedback"]["initial_temperature_c"]) + 273.15,
                        rel_tol=1e-11, abs_tol=1e-15):
        raise ValueError("stack initial temperature differs from the system contract")
    if contract["contract"]["version"] == 2:
        expected = contract["thermal_stack"]["cooling"]["temperature_k"]
        if len(cooling_k) != 1 or not math.isclose(cooling_k[0], expected, rel_tol=1e-11, abs_tol=1e-15):
            raise ValueError("stack cooling temperature differs from the system contract")
    return dict(initial_temperature_k=initial_k, cooling_temperatures_k=cooling_k,
                lower_bound_k=min([initial_k] + cooling_k), rounding_tolerance_k=0.001)


def validate_temperature(temperature_k, bounds, label):
    if not math.isfinite(temperature_k) or temperature_k <= 0:
        raise ValueError(label + ": invalid temperature")
    # Passive conduction, fixed-temperature convection and nonnegative heat
    # sources cannot cool below the minimum initial/boundary temperature.
    # Permit 1 mK for the thermal files' printed precision.
    if temperature_k < bounds["lower_bound_k"] - bounds["rounding_tolerance_k"]:
        raise ValueError("{}: {:.9g} K is below the physical lower bound {:.9g} K".format(
            label, temperature_k, bounds["lower_bound_k"]))


def checked_tmap_statistics(path, cell_count, np, bounds):
    """Validate every raw cell while streaming; retain only extrema and count."""
    minimum, maximum, count = math.inf, -math.inf, 0
    with Path(path).open() as stream:
        for number, line in enumerate(stream, 1):
            line = line.strip()
            if not line or line.startswith(("%", "#")):
                continue
            values = np.asarray(line.split(), dtype=np.float64)
            if len(values) != cell_count or not np.isfinite(values).all():
                raise ValueError(f"{path}:{number}: invalid thermal-cell row")
            low, high = float(values.min()), float(values.max())
            validate_temperature(low, bounds, f"{path}:{number} minimum cell")
            validate_temperature(high, bounds, f"{path}:{number} maximum cell")
            minimum, maximum = min(minimum, low), max(maximum, high)
            count += 1
    if not count:
        raise ValueError(f"{path}: no complete thermal-cell rows")
    return count, minimum, maximum
