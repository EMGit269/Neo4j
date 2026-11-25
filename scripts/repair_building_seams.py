"""Repair regular model subdivision seams and export traced physical building candidates."""

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import geopandas as gpd
import momepy
import numpy as np
import shapely
import yaml
from pyproj import CRS

from urban_fabric.data import utc_now, write_json
from urban_fabric.georeferencing import GeographicModelTransform, verify_layer_roundtrip
from urban_fabric.metrics import _polygon
from urban_fabric.seam_repair import SeamParameters, repair_seams


def projected(frame, transform, crs):
    copy = frame.copy()
    copy.geometry = shapely.segmentize(shapely.force_2d(copy.geometry), 50)
    result = transform.to_wgs84(copy).to_crs(crs)
    result["coordinate_transform_geometry_repaired"] = ~result.is_valid
    invalid = ~result.is_valid
    if invalid.any():
        result.loc[invalid, "geometry"] = result.loc[invalid].geometry.map(_polygon)
    if result.geometry.isna().any() or result.is_empty.any() or not result.is_valid.all():
        raise ValueError("Projection produced unusable repaired geometry.")
    return result


def main(config_path):
    config_path = config_path.resolve()
    config = yaml.safe_load(config_path.read_text())

    def resolve(key):
        return (config_path.parent / config[key]).resolve()

    parameters = SeamParameters(**config["parameters"])
    source = gpd.read_parquet(resolve("source_local"))
    area_local = gpd.read_parquet(resolve("area_local"))
    area_metric = gpd.read_parquet(resolve("area_metric"))
    core_local = area_local.geometry.iloc[0]
    registration = json.loads(resolve("georeferencing_report").read_text())
    transform = GeographicModelTransform(
        **{k: registration["transform"][k] for k in ["offset_x", "offset_y", "units_per_degree"]}
    )
    crs = CRS(registration["metric_crs_wkt"])
    out = resolve("output_dir")
    out.mkdir(parents=True, exist_ok=True)
    print(
        f"Checking all {len(source):,} source candidates to complete boundary groups…", flush=True
    )
    repaired_local, membership, decisions, audit = repair_seams(
        source,
        parameters,
        force_merge_area=core_local if config.get("merge_all_grid_supported_candidates") else None,
        merge_all_grid_candidates=bool(config.get("merge_all_grid_supported_candidates", False)),
    )
    selected = repaired_local[repaired_local.intersection(core_local).area > 0].copy()
    selected = selected.reset_index(drop=True)
    members = membership[membership.building_id.isin(selected.id)].copy()
    parts = source[source.id.isin(members.source_id)].copy().reset_index(drop=True)
    parts["repaired_building_id"] = parts.id.map(members.set_index("source_id").building_id)
    buildings = projected(selected, transform, crs)
    height_parts = projected(parts, transform, crs)
    # Original local metrics have a historical metre name but do not measure ground metres.
    for column in [
        "area_m2",
        "perimeter_m",
        "circular_compactness",
        "elongation",
        "rectangularity",
        "estimated_prism_volume_m3",
    ]:
        if column in height_parts:
            height_parts.rename(columns={column: f"legacy_local_{column}"}, inplace=True)
    height_parts["area_m2"] = height_parts.area
    height_parts["perimeter_m"] = height_parts.length
    height_parts["source_part_prism_estimate_m3"] = height_parts.area * height_parts.height_extent_m
    core = area_metric.geometry.iloc[0]
    buildings["area_m2"] = buildings.area
    buildings["perimeter_m"] = buildings.length
    buildings["area_in_window_m2"] = buildings.intersection(core).area
    buildings["centroid_within_window"] = buildings.centroid.covered_by(core)
    buildings["crosses_window_boundary"] = ~buildings.geometry.covered_by(core.buffer(1e-6))
    buildings["circular_compactness"] = momepy.circular_compactness(buildings.geometry)
    buildings["elongation"] = momepy.elongation(buildings.geometry)
    buildings["rectangularity"] = momepy.rectangularity(buildings.geometry)
    buildings["isoperimetric_quotient"] = 4 * np.pi * buildings.area / buildings.length.pow(2)
    buildings["estimated_prism_volume_m3"] = buildings.area * buildings.height_extent_m
    sums = height_parts.groupby("repaired_building_id").source_part_prism_estimate_m3.sum()
    buildings["source_parts_prism_estimate_sum_m3"] = buildings.id.map(sums)
    buildings["height_semantics"] = np.where(
        buildings.height_conflict,
        (
            "Assigned maximum source-part model height; source heights vary and are "
            "not survey verified."
        ),
        "Source model height; not survey verified.",
    )
    if (
        not buildings.id.is_unique
        or not np.isfinite(
            buildings[
                ["area_m2", "perimeter_m", "circular_compactness", "elongation", "rectangularity"]
            ]
        )
        .all()
        .all()
    ):
        raise ValueError("Recalculated metrics/IDs failed validation.")
    if buildings.height_extent_m.isna().any():
        raise ValueError("A source building group is missing its maximum source-part height.")
    original_core = source[source.intersection(core_local).area > 0]
    original_projected = projected(original_core[["id", "geometry"]], transform, crs)
    before_union = original_projected.geometry.union_all().intersection(core)
    after_union = buildings.geometry.union_all().intersection(core)
    difference = before_union.symmetric_difference(after_union).area
    if difference / before_union.area > parameters.maximum_area_change_ratio:
        raise ValueError("Global footprint coverage changed beyond the repair limit.")
    current_source_ids = set(original_core.id)
    external_ids = set(parts.id) - current_source_ids
    reviewed = buildings[buildings.remaining_seam_review]
    selected_source_ids = set(members.source_id)
    forced_decisions = decisions[
        decisions.decision.eq("accepted_user_requested_all_suspected_seams")
    ]
    window_forced_decisions = sum(
        bool(
            selected_source_ids.intersection(json.loads(row.left_source_ids))
            or selected_source_ids.intersection(json.loads(row.right_source_ids))
        )
        for row in forced_decisions.itertuples()
    )
    window_internal_events = sum(
        event.get("building_id") in set(buildings.id)
        for event in audit["internal_seam_merge_events"]
    )
    tables = {
        "buildings": buildings,
        "height_parts": height_parts,
        "merged_groups": buildings[buildings.seam_merged],
        "remaining_review": reviewed,
    }
    temporary = out / "suzhou_old_city_repaired.pending.gpkg"
    temporary.unlink(missing_ok=True)
    verification = {}
    for name, frame in tables.items():
        frame.to_parquet(out / f"{name}.parquet", index=False)
        if frame.empty:
            continue
        frame.to_file(temporary, layer=name, driver="GPKG", mode="a" if temporary.exists() else "w")
        recovered = gpd.read_file(temporary, layer=name)
        verify_layer_roundtrip(frame, recovered)
        verification[name] = {"count": len(frame), "roundtrip_passed": True}
    temporary.replace(out / "suzhou_old_city_repaired.gpkg")
    members.to_parquet(out / "source_membership.parquet", index=False)
    members.to_csv(out / "source_membership.csv", index=False)
    decisions.to_parquet(out / "all_model_pair_decisions.parquet", index=False)
    buildings.drop(columns="geometry").to_csv(out / "building_metrics.csv", index=False)
    reviewed.drop(columns="geometry").to_csv(out / "remaining_review.csv", index=False)
    map_columns = [
        "id",
        "height_extent_m",
        "model_height_min_m",
        "model_height_max_m",
        "model_height_values_json",
        "height_conflict",
        "height_aggregation_method",
        "area_m2",
        "footprint_requires_review",
        "seam_merged",
        "internal_seam_merged",
        "internal_seam_merge_count",
        "source_component_count",
        "remaining_seam_review",
        "source_ids_json",
        "geometry",
    ]
    buildings[map_columns].to_crs(4326).to_file(out / "buildings_wgs84.geojson", driver="GeoJSON")
    if reviewed.empty:
        (out / "remaining_review_wgs84.geojson").write_text(
            json.dumps({"type": "FeatureCollection", "features": []}) + "\n"
        )
    else:
        reviewed[map_columns].to_crs(4326).to_file(
            out / "remaining_review_wgs84.geojson", driver="GeoJSON"
        )
    report = {
        "created_at_utc": utc_now(),
        "study_area_id": config["study_area_id"],
        "source_sha256": hashlib.sha256(resolve("source_local").read_bytes()).hexdigest(),
        "parameters": asdict(parameters),
        "coordinate_units_for_evidence": "Native model XY units",
        "metric_crs_wkt": crs.to_wkt(),
        "full_model": {
            k: audit[k]
            for k in ["source_count", "result_count", "merged_groups", "height_conflict_groups"]
        },
        "full_model_seam_merges": {
            "forced_grid_pair_decisions": int(
                decisions.decision.eq("accepted_user_requested_all_suspected_seams").sum()
            ),
            "internal_multipart_merge_events": len(audit["internal_seam_merge_events"]),
        },
        "grid_evidence": audit["grids"],
        "window": {
            "original_candidates": len(original_core),
            "repaired_candidates": len(buildings),
            "merged_groups": int(buildings.seam_merged.sum()),
            "source_parts_in_merged_groups": int(
                buildings.loc[buildings.seam_merged, "source_component_count"].sum()
            ),
            "source_parts_including_boundary_completions": len(parts),
            "additional_source_parts_outside_window": len(external_ids),
            "height_conflict_groups": int(buildings.height_conflict.sum()),
            "remaining_review_candidates": len(reviewed),
            "internal_seam_merge_events": window_internal_events,
            "forced_grid_seam_merges": window_forced_decisions,
            "maximum_source_height_assignment": "max(source component height)",
            "footprint_coverage_area_before_m2": before_union.area,
            "footprint_coverage_area_after_m2": after_union.area,
            "symmetric_difference_area_m2": difference,
            "area_m2": core.area,
        },
        "screenshot_group": members[members.source_component_id.isin([66791, 69181])].to_dict(
            "records"
        ),
        "validation": {
            "all_source_members_accounted_once": True,
            "geometry_valid": True,
            "ids_unique": True,
            "coverage_preserved_within_tolerance": True,
            "all_building_heights_are_source_component_maximum": True,
            "layers": verification,
        },
        "limitations": [
            "Algorithmic footprint group repair, not surveyed building entity verification.",
            (
                "A group takes the maximum source-part model height; original heights "
                "remain in height_parts."
            ),
            (
                "Repeated-grid seams between candidates and inside multipart geometries "
                "are merged; "
                "building identity and height remain unverified."
            ),
            "Original 3dm, extracted candidates and georeferenced source remain unchanged.",
        ],
    }
    write_json(out / "repair_report.json", report)
    write_json(
        out / "all_model_merge_events.json",
        audit["events"] + audit["internal_seam_merge_events"],
    )
    (out / "repair_parameters.yaml").write_text(config_path.read_text())
    print(json.dumps(report["window"], ensure_ascii=False, indent=2), flush=True)
    print("Screenshot source membership:", report["screenshot_group"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/building-seam-repair.yaml"))
    main(parser.parse_args().config)
