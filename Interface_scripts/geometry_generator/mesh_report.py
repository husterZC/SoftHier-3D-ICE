"""Describe the generated non-uniform mesh without changing physical inputs."""
import json
import math
import re


def write_mesh_report(document, floorplans, stack_path, pitch):
    dies = {}
    for name, path in floorplans.items():
        regions = []
        for block in path.read_text().split(":")[1:]:
            dimensions = re.search(r"dimension\s+([^,]+),\s*([^;]+);", block)
            counts = re.search(r"discretization\s+(\d+),\s*(\d+)\s*;", block)
            if dimensions is None or counts is None:
                raise ValueError(f"incomplete floorplan region in {path}")
            w, h = map(float, dimensions.groups())
            nx, ny = map(int, counts.groups())
            regions.append((nx * ny, w / nx, h / ny))
        dies[name] = dict(cells=sum(r[0] for r in regions),
                          dx_um=[min(r[1] for r in regions), max(r[1] for r in regions)],
                          dy_um=[min(r[2] for r in regions), max(r[2] for r in regions)])
    first = document["thermal_stack"]["dies"][0]
    w, h = document["geometry"][first["geometry_root"]]["shape"]
    nx, ny = ((math.ceil(w / pitch), math.ceil(h / pitch)) if pitch is not None
              else (int(w / min(w, 50)), int(h / min(h, 50))))
    passive = dict(cells=nx * ny, discretization=[nx, ny], dx_um=[w / nx] * 2, dy_um=[h / ny] * 2)
    layers = [dict(id=layer["id"], thickness_um=layer["thickness_um"],
                   **(dies[layer["die"]] if "die" in layer else passive))
              for layer in document["thermal_stack"]["layers"]]
    result = dict(schema_version=1, requested_pitch_um=pitch, layers=layers,
                  total_cells=sum(layer["cells"] for layer in layers))
    (stack_path.parent / "mesh.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
