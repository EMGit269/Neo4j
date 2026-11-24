"""Acquire OSM building/barrier/entrance features or unsimplified buffered roads."""

import argparse
import json
from importlib.metadata import version

import geopandas as gpd
import osmnx as ox
import pandas as pd
import yaml

from urban_fabric.config import load_config
from urban_fabric.data import utc_now, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--part", choices=["features", "network"], required=True)
    parser.add_argument("--overpass-url", default="https://overpass.private.coffee/api")
    args = parser.parse_args()
    settings = load_config(args.config)
    doc = yaml.safe_load(open(args.config, encoding="utf-8"))
    source = settings.data_dir / "source"
    source.mkdir(parents=True, exist_ok=True)
    area = gpd.read_file(settings.polygon_path).to_crs(settings.metric_crs)
    area["id"] = settings.study_area_id
    buffer_m = doc["cleaning"]["context_buffer_m"]
    context = gpd.GeoDataFrame(geometry=area.geometry.buffer(buffer_m), crs=area.crs)
    area.to_parquet(source / "area.parquet", index=False)
    context.to_parquet(source / "context.parquet", index=False)
    ox.settings.use_cache = True
    ox.settings.cache_folder = settings.data_dir / "cache"
    ox.settings.requests_timeout = settings.timeout_seconds
    ox.settings.log_console = True
    ox.settings.overpass_url = args.overpass_url
    ox.settings.http_user_agent = "urban-fabric-lab/0.1 (academic Nanjing morphology pilot; OSMnx)"
    polygon = context.to_crs(4326).geometry.union_all()
    target = source / (
        "osm_features.parquet" if args.part == "features" else "osm_unsimplified.graphml"
    )
    if target.exists():
        print("Existing acquired source preserved:", target)
        return
    if args.part == "features":
        features = ox.features_from_polygon(
            polygon, tags={"building": True, "entrance": True, "barrier": True}
        ).reset_index()
        # OSM tag schemas are heterogeneous. Keep lossless text for tags, typed geometry and IDs.
        for column in features.columns:
            if column in {features.geometry.name, "osmid"}:
                continue

            def text(value):
                if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
                    return None
                return (
                    json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (list, dict))
                    else str(value)
                )

            features[column] = features[column].map(text)
        features.to_parquet(target, index=False)
        counts = {"features": len(features), "columns": list(features.columns)}
    else:
        ox.settings.useful_tags_node = sorted(
            set(ox.settings.useful_tags_node)
            | {"entrance", "access", "barrier", "crossing", "foot", "wheelchair", "layer"}
        )
        ox.settings.useful_tags_way = sorted(
            set(ox.settings.useful_tags_way)
            | {
                "access",
                "foot",
                "foot:conditional",
                "access:conditional",
                "bridge",
                "tunnel",
                "layer",
                "service",
                "oneway:foot",
                "footway",
                "sidewalk",
                "covered",
                "area",
            }
        )
        graph = ox.graph_from_polygon(
            polygon, network_type="walk", simplify=False, retain_all=True, truncate_by_edge=True
        )
        ox.save_graphml(graph, target)
        counts = {"nodes": len(graph), "directed_edges": graph.number_of_edges()}
    write_json(
        source / (args.part + "_acquisition.json"),
        {
            "source": "OpenStreetMap",
            "acquired_at_utc": utc_now(),
            "osmnx_version": version("osmnx"),
            "license": "ODbL-1.0",
            "attribution": "© OpenStreetMap contributors",
            "context_buffer_m": buffer_m,
            "query_bounds": list(polygon.bounds),
            "part": args.part,
            "counts": counts,
            "overpass_url": args.overpass_url,
            "semantics": (
                "Mapped geometry/tags, not surveyed completeness or verified public access."
            ),
        },
    )


if __name__ == "__main__":
    main()
