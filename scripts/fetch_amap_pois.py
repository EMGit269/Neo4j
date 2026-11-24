"""Prepare or execute a bounded 500 m POI pilot; no key or API call needed for dry-run."""

import argparse
import json
import os
from pathlib import Path

import geopandas as gpd
import pandas as pd
from dotenv import load_dotenv
from pyproj import Geod

from urban_fabric.amap import AmapClient, AmapError
from urban_fabric.data import write_json


def main(args):
    area = gpd.read_file(args.area)
    center = area.to_crs(3857).centroid.to_crs(4326).iloc[0]
    geod = Geod(ellps="WGS84")
    west = geod.fwd(center.x, center.y, 270, args.pilot_size_m / 2)[0]
    east = geod.fwd(center.x, center.y, 90, args.pilot_size_m / 2)[0]
    south = geod.fwd(center.x, center.y, 180, args.pilot_size_m / 2)[1]
    north = geod.fwd(center.x, center.y, 0, args.pilot_size_m / 2)[1]
    ring = [(west, north), (east, north), (east, south), (west, south), (west, north)]
    out = args.output
    plan = {
        "mode": "execute" if args.execute else "dry_run_no_network",
        "pilot_size_m": args.pilot_size_m,
        "query_ring_wgs84": ring,
        "types": args.types,
        "max_pages": args.max_pages,
        "max_requests": args.max_requests,
        "coordinate_conversion": "Official /v3/assistant/coordinate/convert coordsys=gps",
        "search": "Official /v5/place/polygon (GCJ-02 query)",
        "result_coordinates": "Raw GCJ-02; no falsely labelled WGS84 export",
        "completeness": "Small sample, not a full POI inventory.",
    }
    write_json(out / "query_plan.json", plan)
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return
    load_dotenv()
    client = AmapClient(
        os.environ.get("AMAP_WEB_KEY", ""),
        out / "raw",
        max_requests=args.max_requests,
        interval=args.interval,
    )
    try:
        gcj_ring = client.wgs84_to_gcj02(ring)
        # The repeated first/last vertex must stay exactly identical after conversion.
        gcj_ring[-1] = gcj_ring[0]
        pois, manifest = client.polygon_sample(gcj_ring, types=args.types, max_pages=args.max_pages)
        write_json(out / "pois_raw_gcj02.json", pois)
        rows = []
        for poi in pois:
            values = {
                key: poi.get(key)
                for key in [
                    "id",
                    "name",
                    "type",
                    "typecode",
                    "address",
                    "adname",
                    "adcode",
                    "cityname",
                    "citycode",
                ]
            }
            location = poi.get("location")
            values["location_gcj02"] = location
            if isinstance(location, str) and "," in location:
                values["longitude_gcj02"], values["latitude_gcj02"] = map(
                    float, location.split(",")
                )
            values["coordinate_reference"] = "GCJ-02"
            rows.append(values)
        columns = [
            "id",
            "name",
            "type",
            "typecode",
            "address",
            "adname",
            "adcode",
            "cityname",
            "citycode",
            "location_gcj02",
            "longitude_gcj02",
            "latitude_gcj02",
            "coordinate_reference",
        ]
        pd.DataFrame(rows).reindex(columns=columns).to_csv(out / "pois_gcj02.csv", index=False)
        manifest.update({"query_ring_gcj02": gcj_ring, "plan": plan})
        write_json(out / "manifest.json", manifest)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    except AmapError as error:
        write_json(out / "failure.json", {"error": str(error), "requests_attempted": client.calls})
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--area",
        type=Path,
        default=Path("data/suzhou_old_city/georeferenced/analysis_window_wgs84.geojson"),
    )
    parser.add_argument("--output", type=Path, default=Path("data/suzhou_old_city/amap_pilot"))
    parser.add_argument("--types", default="140000")
    parser.add_argument("--pilot-size-m", type=float, default=500)
    parser.add_argument("--max-pages", type=int, default=2)
    parser.add_argument("--max-requests", type=int, default=4)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not 0 < args.pilot_size_m <= 1000:
        parser.error("本预实验限定边长 0–1000 m；扩大研究需先制定分类和抽样方案。")
    try:
        main(args)
    except (AmapError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
