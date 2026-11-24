"""Inspect a 3dm and extract local-coordinate building candidates without modifying it."""

import argparse
import json
from pathlib import Path

from urban_fabric.rhino import export_local_geopackage, inspect_model

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--inspect-only", action="store_true")
    parser.add_argument("--geopackage", action="store_true")
    args = parser.parse_args()
    report = inspect_model(
        args.model,
        args.output,
        extract=not args.inspect_only,
        progress=lambda message: print(message, flush=True),
    )
    if args.geopackage and not args.inspect_only:
        print(json.dumps(export_local_geopackage(args.output), ensure_ascii=False), flush=True)
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in ["layers", "layer_bounds_xyz"]},
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
