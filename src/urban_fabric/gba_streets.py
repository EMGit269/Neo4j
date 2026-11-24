"""Enrich an acquired GBA sample with a separately sourced OSM street network."""

import json
from importlib.metadata import version
from pathlib import Path

import geopandas as gpd

from .config import Settings
from .data import Layers, utc_now, write_json
from .fetch import fetch_street_network
from .graph import export_graph
from .metrics import analyze


def add_osm_streets(directory: Path, *, timeout=180):
    root = directory.resolve()
    archive = root / "source/gba-summary.json"
    summary_path = root / "processed/summary.json"
    original = json.loads((archive if archive.exists() else summary_path).read_text())
    if original["source_manifest"]["source"] != "GlobalBuildingAtlas":
        raise ValueError("请提供已获取的 GBA 数据目录。")
    if not archive.exists():
        write_json(archive, original)
    area = gpd.read_parquet(root / "processed/area.parquet")
    buildings = gpd.read_parquet(root / "processed/buildings.parquet")
    buildings["source"] = "GlobalBuildingAtlas"
    area["source"] = "OpenStreetMap"
    settings = Settings(
        study_area_id=original["study_area_id"],
        data_dir=root,
        polygon_path=root / "raw/area.geojson",
        timeout_seconds=timeout,
        metric_crs=original["metric_crs"],
    )
    print("Fetching OSM walk network for the existing GBA study area...", flush=True)
    nodes, segments = fetch_street_network(settings, area)
    nodes["source"] = "OpenStreetMap"
    segments["source"] = "OpenStreetMap"
    street_source = {
        "source": "OpenStreetMap",
        "acquired_at_utc": utc_now(),
        "osmnx_version": version("osmnx"),
        "network_type": "walk",
        "license": "ODbL-1.0",
        "attribution": "© OpenStreetMap contributors",
        "query_bounds_wgs84": area.to_crs(4326).total_bounds.tolist(),
        "raw_street_nodes": len(nodes),
        "raw_physical_segments": len(segments),
        "semantics": "Undirected physical centerlines; not verified pedestrian access.",
    }
    Layers(area, buildings, nodes, segments).save(root / "raw")
    provenance = {
        **original["source_manifest"],
        "schema_version": 1,
        "study_area_id": original["study_area_id"],
        "source": "GlobalBuildingAtlas + OpenStreetMap",
        "street_source": street_source,
    }
    write_json(root / "raw/manifest.json", provenance)
    layers, morphology = analyze(settings)
    # Keep the GBA height/source statistics together with the newly computed network statistics.
    result = {
        **original,
        **morphology,
        "partial_building_sample": original["partial_building_sample"],
        "street_data_added_at_utc": street_source["acquired_at_utc"],
        "warnings": [w for w in original["warnings"] if "did not acquire OSM streets" not in w]
        + morphology["warnings"],
    }
    write_json(summary_path, result)
    layers.buildings.to_crs(4326).to_file(root / "processed/buildings.geojson", driver="GeoJSON")
    layers.buildings.to_file(root / "processed/buildings.gpkg", layer="buildings", driver="GPKG")
    layers.buildings.drop(columns="geometry").to_csv(
        root / "processed/building_heights.csv", index=False, encoding="utf-8-sig"
    )
    layers.street_segments.to_crs(4326).to_file(
        root / "processed/street_segments.geojson", driver="GeoJSON"
    )
    layers.street_segments.to_file(root / "processed/streets.gpkg", layer="streets", driver="GPKG")
    layers.street_nodes.to_file(root / "processed/streets.gpkg", layer="nodes", driver="GPKG")
    write_json(root / "source/street_manifest.json", street_source)
    result["graph"] = export_graph(settings)
    return result
