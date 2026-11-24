"""Select a local-coordinate study window while retaining whole building footprints."""

import argparse
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import yaml
from shapely.geometry import box

from urban_fabric.data import utc_now, write_json
from urban_fabric.rhino import export_local_geopackage


def select(config_path):
    config_path = Path(config_path).resolve()
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    source = (config_path.parent / config["paths"]["source_dir"]).resolve()
    target = (config_path.parent / config["paths"]["data_dir"]).resolve()
    west, south, east, north = config["study_area"]["bbox_local_m"]
    if not (np.isfinite([west, south, east, north]).all() and west < east and south < north):
        raise ValueError("bbox_local_m must be finite [xmin, ymin, xmax, ymax].")
    window = box(west, south, east, north)
    buildings = gpd.read_parquet(source / "building_candidates.parquet")
    linework = gpd.read_parquet(source / "context_linework.parquet")
    if not buildings.crs.is_engineering or linework.crs != buildings.crs:
        raise ValueError("Expected consistent local engineering CRS.")
    buildings = buildings[buildings.intersects(window)].copy().reset_index(drop=True)
    clipped_area = buildings.geometry.intersection(window).area
    buildings = buildings[clipped_area > 0].copy().reset_index(drop=True)
    buildings["area_in_window_m2"] = buildings.geometry.intersection(window).area
    buildings["centroid_within_window"] = buildings.centroid.covered_by(window)
    buildings["crosses_window_boundary"] = ~buildings.geometry.covered_by(window)
    # Geometric shape and height refer to full source objects, never truncated footprints.
    selected_lines = linework[linework.intersects(window)].copy().reset_index(drop=True)
    selected_lines["length_in_window_m"] = selected_lines.geometry.intersection(window).length
    clipped_lines = selected_lines.copy()
    clipped_lines.geometry = clipped_lines.geometry.intersection(window)
    clipped_lines = clipped_lines[clipped_lines.geometry.length > 0].reset_index(drop=True)
    clipped_lines["length_m"] = clipped_lines.geometry.length
    area = gpd.GeoDataFrame(
        {"id": [config["study_area"]["id"]], "name": [config["study_area"]["name"]]},
        geometry=[window],
        crs=buildings.crs,
    )
    target.mkdir(parents=True, exist_ok=True)
    area.to_parquet(target / "area.parquet", index=False)
    buildings.to_parquet(target / "building_candidates.parquet", index=False)
    buildings.drop(columns="geometry").to_csv(
        target / "building_candidate_metrics.csv", index=False
    )
    selected_lines.to_parquet(target / "context_linework.parquet", index=False)
    clipped_lines.to_parquet(target / "clipped_linework.parquet", index=False)
    gpkg = export_local_geopackage(target, name="suzhou_old_city_local.gpkg")
    destination = target / "suzhou_old_city_local.gpkg"
    area.to_file(destination, layer="analysis_window", driver="GPKG", mode="a")
    for role in sorted(clipped_lines.role.unique()):
        clipped_lines[clipped_lines.role == role].to_file(
            destination, layer=f"clipped_{role}", driver="GPKG", mode="a"
        )
    recovered_area = gpd.read_file(destination, layer="analysis_window")
    if not recovered_area.geometry.iloc[0].equals(window):
        raise ValueError("Analysis window GeoPackage roundtrip failed.")
    layer_counts = {}
    for role in sorted(selected_lines.role.unique()):
        full = gpd.read_file(destination, layer=role)
        cropped = gpd.read_file(destination, layer=f"clipped_{role}")
        expected_full = selected_lines[selected_lines.role == role]
        expected_cropped = clipped_lines[clipped_lines.role == role]
        if (
            len(full) != len(expected_full)
            or len(cropped) != len(expected_cropped)
            or not full.id.is_unique
            or not cropped.id.is_unique
        ):
            raise ValueError(
                "GeoPackage linework roundtrip failed or repeated export duplicated rows."
            )
        if not cropped.geometry.covered_by(window.buffer(1e-8)).all():
            raise ValueError("Clipped linework extends beyond the selected window.")
        layer_counts[role] = {"source_objects": len(full), "clipped_objects": len(cropped)}
    gpkg["layers"] = len(gpd.list_layers(destination))
    gpkg["linework_roundtrip_passed"] = True
    gpkg["linework_counts"] = layer_counts
    if not all(buildings.geometry.is_valid) or not buildings.id.is_unique:
        raise ValueError("Invalid/duplicate candidate footprints in selected area.")
    union_area = buildings.geometry.union_all().intersection(window).area
    source_report = json.loads((source / "report.json").read_text())
    report = {
        "selected_at_utc": utc_now(),
        "study_area_id": config["study_area"]["id"],
        "name": config["study_area"]["name"],
        "source_dir": str(source),
        "source_sha256": source_report["source_sha256"],
        "source_model_report": str(source / "report.json"),
        "bbox_local_m": [west, south, east, north],
        "bbox_local_km": [west / 1000, south / 1000, east / 1000, north / 1000],
        "window_area_model_units2": window.area,
        "xy_ground_metres_verified": False,
        "metric_basis": "Raw model units; use calibrated projected exports for physical metrics.",
        "building_candidates": len(buildings),
        "centroids_within_window": int(buildings.centroid_within_window.sum()),
        "boundary_crossing_candidates": int(buildings.crosses_window_boundary.sum()),
        "footprint_union_area_in_window_m2": float(union_area),
        "model_footprint_coverage_ratio": float(union_area / window.area),
        "height_extent_quantiles_m": {
            str(p): float(buildings.height_extent_m.quantile(p))
            for p in [0, 0.25, 0.5, 0.75, 0.95, 1]
        },
        "footprint_review_candidates": int(buildings.footprint_requires_review.sum()),
        "source_linework_counts": {
            str(k): int(v) for k, v in selected_lines.role.value_counts().items()
        },
        "geopackage_verification": gpkg,
        "georeferencing_verified": False,
        "road_network_topology_verified": False,
        "window_basis": "User-selected local rectangle; not a verified ancient-city boundary.",
        "geometry_policy": (
            "Whole buildings/selected source lines retained; clipped linework also saved."
        ),
        "height_semantics": "Model vertical extent; heights quantized in 3m steps, not surveyed.",
    }
    write_json(target / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    select(parser.parse_args().config)
