"""Calibrate the Suzhou model against downloaded SHPs and export the selected window."""

import argparse
import hashlib
import json
from pathlib import Path

import geopandas as gpd
import momepy
import numpy as np
import pandas as pd
import shapely
import yaml
from pyproj import CRS, Geod

from urban_fabric.data import utc_now, write_json
from urban_fabric.georeferencing import calibrate_geographic_model, verify_layer_roundtrip
from urban_fabric.metrics import _polygon


def source_record(path):
    files = {}
    for suffix in [".shp", ".shx", ".dbf", ".prj"]:
        item = path.with_suffix(suffix)
        with item.open("rb") as stream:
            files[item.name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": str(path), "sha256": files}


def main(args):
    config_path = args.config.resolve()
    config = yaml.safe_load(config_path.read_text())
    model_dir = (config_path.parent / config["paths"]["source_dir"]).resolve()
    selected_dir = (config_path.parent / config["paths"]["data_dir"]).resolve()
    out = selected_dir / "georeferenced"
    out.mkdir(parents=True, exist_ok=True)
    roads_path = args.shp_dir / "gis_osm_roads_free_1.shp"
    reference_path = args.shp_dir / "Suzhou_Buildings.shp"
    roads = gpd.read_file(roads_path)
    roads["id"] = roads.osm_id.astype(str)
    reference = gpd.read_file(reference_path)
    reference["source_fid"] = reference.index
    reference.geometry = shapely.force_2d(reference.geometry)
    metric_crs = CRS(reference.crs)
    if not metric_crs.is_projected or metric_crs.axis_info[0].unit_conversion_factor != 1:
        raise ValueError("Building reference must declare a projected metre CRS.")
    local_lines = gpd.read_parquet(model_dir / "context_linework.parquet")
    transform, controls, counts = calibrate_geographic_model(
        local_lines[local_lines.role == "road_linework"], roads
    )
    controls.to_parquet(out / "road_control_segments.parquet", index=False)
    print("Calibrated", transform, counts, flush=True)

    layers = {}
    repairs = {}
    for name, filename in [
        ("analysis_window", "area.parquet"),
        ("buildings", "building_candidates.parquet"),
        ("context_linework", "context_linework.parquet"),
        ("clipped_linework", "clipped_linework.parquet"),
    ]:
        local = gpd.read_parquet(selected_dir / filename)
        # Keep the original model metrics explicitly separate from physical measurements.
        metric_columns = [
            "area_m2",
            "perimeter_m",
            "area_in_window_m2",
            "length_model_m",
            "length_m",
            "length_in_window_m",
            "estimated_prism_volume_m3",
            "circular_compactness",
            "elongation",
            "rectangularity",
        ]
        local = local.rename(columns={c: f"legacy_local_{c}" for c in metric_columns if c in local})
        local.geometry = shapely.segmentize(shapely.force_2d(local.geometry), 50)
        frame = transform.to_wgs84(local).to_crs(metric_crs)
        invalid = ~frame.geometry.is_valid
        repairs[name] = int(invalid.sum())
        frame["coordinate_transform_geometry_repaired"] = invalid
        if invalid.any():
            if name == "buildings":
                frame.loc[invalid, "geometry"] = frame.loc[invalid].geometry.map(_polygon)
            else:
                frame.loc[invalid, "geometry"] = frame.loc[invalid].geometry.make_valid()
        if frame.geometry.isna().any() or frame.geometry.is_empty.any():
            raise ValueError(f"Transformation lost geometry in {name}.")
        frame["georeferencing_method"] = "scaled_WGS84_geographic_model_calibrated_to_OSM_SHP"
        layers[name] = frame

    area = layers["analysis_window"].geometry.iloc[0]
    buildings = layers["buildings"]
    buildings["area_m2"] = buildings.area
    buildings["perimeter_m"] = buildings.length
    buildings["area_in_window_m2"] = buildings.geometry.intersection(area).area
    buildings["circular_compactness"] = momepy.circular_compactness(buildings.geometry)
    buildings["elongation"] = momepy.elongation(buildings.geometry)
    buildings["rectangularity"] = momepy.rectangularity(buildings.geometry)
    buildings["estimated_prism_volume_m3"] = buildings.area_m2 * buildings.height_extent_m
    # Re-evaluate boundary membership in physical coordinates; IDs/whole objects remain intact.
    buildings["centroid_within_window"] = buildings.centroid.covered_by(area)
    buildings["crosses_window_boundary"] = ~buildings.geometry.covered_by(area.buffer(1e-6))
    for key in ["context_linework", "clipped_linework"]:
        layers[key]["length_m"] = layers[key].length
        layers[key]["length_in_window_m"] = layers[key].intersection(area).length
    metric_columns = [
        "area_m2",
        "perimeter_m",
        "circular_compactness",
        "elongation",
        "rectangularity",
    ]
    if not np.isfinite(buildings[metric_columns].to_numpy()).all():
        raise ValueError("Non-finite recalculated building metrics.")
    if not buildings.id.is_unique or not buildings.is_valid.all():
        raise ValueError("Transformed building IDs/geometries failed validation.")

    # Independent SHP comparison: report agreement without calling either source survey truth.
    reference_metric = reference.to_crs(metric_crs)
    reference_metric = reference_metric[reference_metric.intersects(area.buffer(500))].copy()
    reference_invalid = ~reference_metric.is_valid
    reference_metric["reference_geometry_repaired"] = reference_invalid
    reference_metric.loc[reference_invalid, "geometry"] = reference_metric.loc[
        reference_invalid
    ].geometry.map(_polygon)
    reference_metric = reference_metric[reference_metric.geometry.notna()].copy()
    reference_metric = reference_metric.drop_duplicates(subset="geometry").reset_index(drop=True)
    from scipy.spatial import cKDTree

    d, indices = cKDTree(shapely.get_coordinates(reference_metric.centroid)).query(
        shapely.get_coordinates(buildings.centroid)
    )
    right = reference_metric.geometry.to_numpy()[indices]
    intersection = shapely.area(shapely.intersection(buildings.geometry.to_numpy(), right))
    union = buildings.area.to_numpy() + shapely.area(right) - intersection
    comparisons = pd.DataFrame(
        {
            "building_id": buildings.id,
            "reference_fid": reference_metric.source_fid.to_numpy()[indices],
            "centroid_distance_m": d,
            "footprint_iou": intersection / union,
        }
    )
    comparisons.to_parquet(out / "building_reference_comparison.parquet", index=False)
    layers["reference_buildings_shp"] = reference_metric
    layers["reference_osm_roads"] = roads.to_crs(metric_crs)
    layers["reference_osm_roads"] = layers["reference_osm_roads"][
        layers["reference_osm_roads"].intersects(area.buffer(500))
    ].copy()
    for key, file in [
        ("reference_osm_water", "gis_osm_water_a_free_1.shp"),
        ("reference_osm_pois", "gis_osm_pois_free_1.shp"),
        ("reference_osm_poi_areas", "gis_osm_pois_a_free_1.shp"),
    ]:
        path = args.shp_dir / file
        if path.exists():
            frame = gpd.read_file(path).to_crs(metric_crs)
            layers[key] = frame[frame.intersects(area.buffer(500))].copy()

    gpkg = out / "suzhou_old_city_projected.pending.gpkg"
    if gpkg.exists():
        gpkg.unlink()
    verification = {}
    for name, frame in layers.items():
        frame.to_parquet(out / f"{name}.parquet", index=False)
        frame.to_file(gpkg, layer=name, driver="GPKG", mode="a" if gpkg.exists() else "w")
        recovered = gpd.read_file(gpkg, layer=name)
        verify_layer_roundtrip(frame, recovered)
        verification[name] = {
            "count": len(frame),
            "roundtrip_passed": True,
            "geometry_topology_equal": True,
        }
    gpkg.replace(out / "suzhou_old_city_projected.gpkg")
    buildings.drop(columns="geometry").to_csv(out / "building_metrics.csv", index=False)
    for key in [
        "analysis_window",
        "buildings",
        "context_linework",
        "reference_osm_roads",
        "reference_osm_water",
        "reference_osm_pois",
        "reference_osm_poi_areas",
    ]:
        if key in layers:
            # GeoJSON carries WGS84 for web maps and POI queries, never custom projected XY.
            columns = [
                c
                for c in [
                    "id",
                    "osm_id",
                    "name",
                    "role",
                    "height_extent_m",
                    "fclass",
                    "area_m2",
                    "footprint_requires_review",
                ]
                if c in layers[key]
            ]
            layers[key][[*columns, "geometry"]].to_crs(4326).to_file(
                out / f"{key}_wgs84.geojson", driver="GeoJSON"
            )
    bounds = layers["analysis_window"].to_crs(4326).total_bounds.tolist()
    geod = Geod(ellps="WGS84")
    geo_area, _ = geod.geometry_area_perimeter(
        layers["analysis_window"].to_crs(4326).geometry.iloc[0]
    )
    width = geod.inv(bounds[0], sum(bounds[1::2]) / 2, bounds[2], sum(bounds[1::2]) / 2)[2]
    height = geod.inv(sum(bounds[::2]) / 2, bounds[1], sum(bounds[::2]) / 2, bounds[3])[2]
    residuals = {}
    for partition in ["fit", "validation"]:
        part = controls[controls.partition == partition]
        errors = part[["residual_m", "end_residual_m"]].to_numpy().ravel()
        residuals[partition] = {
            "segments": len(part),
            "median_m": float(np.median(errors)),
            "p95_m": float(np.quantile(errors, 0.95)),
            "max_m": float(max(errors)),
        }
    union_area = buildings.geometry.union_all().intersection(area).area
    report = {
        "created_at_utc": utc_now(),
        "study_area_id": config["study_area"]["id"],
        "model_source": json.loads((model_dir / "report.json").read_text())["source_sha256"],
        "sources": [source_record(roads_path), source_record(reference_path)],
        "georeferencing_verified": True,
        "verification_scope": "Coordinate reconstruction against OSM SHP, not surveyed accuracy.",
        "transform": {
            "offset_x": transform.offset_x,
            "offset_y": transform.offset_y,
            "units_per_degree": transform.units_per_degree,
            "forward": "lon=(x+offset_x)/units_per_degree; lat=(y+offset_y)/units_per_degree",
            "inverse": "x=lon*units_per_degree-offset_x; y=lat*units_per_degree-offset_y",
            "z_policy": "2D exports; original model height attributes retained without scaling.",
        },
        "metric_crs_wkt": metric_crs.to_wkt(),
        "metric_crs_note": "Original SHP UTM_Modified WKT; not EPSG:32651.",
        "controls": {
            **counts,
            "source_road_objects": int(controls.source_id_local.nunique()),
            "reference_road_objects": int(controls.source_id_reference.nunique()),
            "local_bounds": [
                float(controls.x_local.min()),
                float(controls.y_local.min()),
                float(controls.x_local.max()),
                float(controls.y_local.max()),
            ],
            "residuals": residuals,
            "partition": "Whole 0.02-degree tiles held out; tile residue mod 5.",
            "precision_note": (
                "Near-zero residuals recover shared-source geometry, not survey accuracy."
            ),
        },
        "bbox_local_model_units": config["study_area"]["bbox_local_m"],
        "bbox_wgs84_wsen": bounds,
        "window_area_projected_km2": area.area / 1e6,
        "window_area_geodesic_km2": abs(geo_area) / 1e6,
        "approx_width_height_m": [width, height],
        "legacy_local_area_not_ground_km2": 30.0,
        "building_candidates": len(buildings),
        "footprint_union_area_in_window_m2": union_area,
        "building_coverage_ratio": union_area / area.area,
        "coordinate_transform_geometry_repairs": repairs,
        "reference_building_comparison": {
            "method": "Nearest centroids; descriptive only, no building identity assumed.",
            "centroid_distance_m_quantiles": {
                str(q): float(np.quantile(d, q)) for q in [0.1, 0.5, 0.9, 0.95]
            },
            "iou_quantiles": {
                str(q): float(comparisons.footprint_iou.quantile(q)) for q in [0.1, 0.5, 0.9, 0.95]
            },
            "iou_above_0_8": int((comparisons.footprint_iou > 0.8).sum()),
        },
        "geopackage_verification": verification,
        "height_is_survey_verified": False,
        "road_network_topology_verified": False,
        "warnings": [
            "Legacy _m/_m2 fields measure model units; use recalculated export fields.",
            "SHP dates are not verified observation dates; vintage/segmentation may differ.",
            "POIs in GCJ-02 require explicit datum handling before spatial joins.",
            "Preserved model heights are not made accurate by horizontal georeferencing.",
        ],
    }
    write_json(out / "georeferencing.json", report)
    # The raw files stay in model coordinates; point readers to the calibrated exports.
    for raw_dir in [model_dir, selected_dir]:
        raw_report_path = raw_dir / "report.json"
        raw_report = json.loads(raw_report_path.read_text())
        raw_report["georeferenced_export_available"] = True
        raw_report["georeferencing_report"] = str(out / "georeferencing.json")
        raw_report["xy_ground_metres_verified"] = False
        raw_report["coordinate_semantics"] = (
            "Raw XY is scaled WGS84 degrees minus offset; legacy _m fields measure model units."
        )
        if raw_dir == selected_dir:
            raw_report.pop("window_area_km2", None)
            raw_report["window_area_model_units2"] = (
                config["study_area"]["bbox_local_m"][2] - config["study_area"]["bbox_local_m"][0]
            ) * (config["study_area"]["bbox_local_m"][3] - config["study_area"]["bbox_local_m"][1])
            raw_report["window_area_ground_projected_km2"] = report["window_area_projected_km2"]
        write_json(raw_report_path, raw_report)
    print(
        json.dumps(
            {
                k: report[k]
                for k in [
                    "bbox_wgs84_wsen",
                    "window_area_projected_km2",
                    "approx_width_height_m",
                    "building_candidates",
                    "coordinate_transform_geometry_repairs",
                    "controls",
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/suzhou-old-city.yaml"))
    parser.add_argument("--shp-dir", type=Path, required=True)
    main(parser.parse_args())
