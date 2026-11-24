"""Screen OSM POIs in the exact core, retaining raw features and a complete decision audit."""

import argparse
import hashlib
import json
from pathlib import Path

import geopandas as gpd
import yaml

from urban_fabric.data import utc_now, write_json
from urban_fabric.georeferencing import verify_layer_roundtrip
from urban_fabric.pois import filter_features


def main(args):
    config = args.config.resolve()
    rules = yaml.safe_load(config.read_text())
    out = (config.parent / rules["output_dir"]).resolve()
    manifest = json.loads((out / "source/acquisition.json").read_text())
    if rules["query_tags"] != manifest["request_spec"]["tags_union"]:
        raise ValueError(
            "Query tag rules changed since acquisition; source coverage is inconsistent."
        )
    source_path = out / "source/osm_features_wgs84.parquet"
    if hashlib.sha256(source_path.read_bytes()).hexdigest() != manifest["source_sha256"]:
        raise ValueError("OSM source checksum changed.")
    source = gpd.read_parquet(source_path)
    area = gpd.read_parquet((config.parent / rules["metric_reference"]).resolve())
    audit, members, features, points = filter_features(source, area, rules)
    mapping = members.set_index("osm_uid")
    audit["duplicate_of"] = audit.osm_uid.map(mapping.duplicate_of)
    duplicates = audit.duplicate_of.notna()
    audit.loc[duplicates, "decision"] = "duplicate"
    files = {
        "all_candidates": audit,
        "poi_members": members,
        "poi_features": features,
        "poi_points": points,
        "excluded": audit[audit.decision == "excluded"],
        "context": audit[audit.decision == "context"],
        "review": audit[audit.decision == "review"],
        "duplicates": audit[audit.decision == "duplicate"],
    }
    for name, frame in files.items():
        frame.to_parquet(out / f"{name}.parquet", index=False)
        frame.drop(columns="geometry").to_csv(out / f"{name}.csv", index=False)
    points.to_crs(4326).to_file(out / "poi_points_wgs84.geojson", driver="GeoJSON")
    # Small, stable fields for the interactive map; full source tags remain in GIS/CSV.
    columns = [
        "osm_uid",
        "name",
        "category",
        "category_code",
        "subcategory",
        "poi_role",
        "member_count",
        "source_geometry_type",
        "access_raw",
        "geometry",
    ]
    points[columns].to_crs(4326).to_file(out / "poi_map_wgs84.geojson", driver="GeoJSON")
    destination = out / "suzhou_old_city_osm_pois.gpkg"
    temporary = destination.with_name(destination.stem + ".pending.gpkg")
    temporary.unlink(missing_ok=True)
    for name in ["poi_points", "poi_features", "review", "excluded"]:
        frame = files[name]
        if frame.empty:
            continue
        frame.to_file(temporary, layer=name, driver="GPKG", mode="a" if temporary.exists() else "w")
        recovered = gpd.read_file(temporary, layer=name)
        verify_layer_roundtrip(frame, recovered)
        if recovered.osm_uid.tolist() != frame.osm_uid.tolist():
            raise ValueError(f"POI IDs changed in {name} roundtrip.")
    temporary.replace(destination)
    categories = points.groupby(["category_code", "category"]).size().reset_index(name="poi_count")
    categories = categories.sort_values("poi_count", ascending=False)
    categories.to_csv(out / "category_counts.csv", index=False)
    reasons = (
        audit.groupby(["decision", "filter_reason", "filter_reason_zh"])
        .size()
        .reset_index(name="count")
    )
    reasons.to_csv(out / "filter_reason_counts.csv", index=False)
    summary = {
        "processed_at_utc": utc_now(),
        "study_area": rules["study_area"],
        "core_bbox_wgs84": list(area.to_crs(4326).total_bounds),
        "source_manifest": manifest,
        "rules_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "counts": {
            "source_features": len(source),
            "kept_unique_pois": len(points),
            "kept_source_members": len(members),
            "duplicates": int(duplicates.sum()),
            "excluded": len(files["excluded"]),
            "context": len(files["context"]),
            "review": len(files["review"]),
        },
        "categories": categories.to_dict("records"),
        "poi_roles": points.poi_role.value_counts().to_dict(),
        "quality": {
            "missing_name": int(points.name.eq("").sum()),
            "ambiguous_duplicate_review": int(points.ambiguous_duplicate_review.sum()),
            "boundary_crossing_features": int(points.crosses_core_boundary.sum()),
            "repaired_by_local_filter": int(audit.geometry_repaired_in_filter.sum()),
            "all_representative_points_in_core": True,
            "poi_uid_unique": bool(points.osm_uid.is_unique),
            "geopackage_roundtrip_passed": True,
        },
        "semantics": [
            (
                "OSM features matching a documented union of tags, not an "
                "exhaustive real-world census."
            ),
            (
                "Area and line POIs use an internal representative point within "
                "the core, not a real entrance."
            ),
            "Station, platform, stop-position and entrance roles remain distinct.",
            (
                "Unnamed places retained when mapped with an explicit function; "
                "public access not assumed."
            ),
            (
                "Point/area duplicates require matching identity and geometry; "
                "disjoint branches retained."
            ),
            "Functional-building-only and unknown functions are in a separate review list.",
        ],
    }
    if sum(
        summary["counts"][key]
        for key in ["kept_unique_pois", "duplicates", "excluded", "context", "review"]
    ) != len(source):
        raise ValueError("Audit decisions do not account for every source feature.")
    write_json(out / "summary.json", summary)
    (out / "filter_rules.yaml").write_text(config.read_text())
    print(
        json.dumps(
            {k: summary[k] for k in ["counts", "categories", "poi_roles", "quality"]},
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/osm-poi-filter.yaml"))
    main(parser.parse_args())
