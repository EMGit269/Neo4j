"""Stream official GBA polygons and height attributes into a district dataset."""

import json
import math
import time
from importlib.metadata import version
from pathlib import Path

import geopandas as gpd
import ijson
from shapely import make_valid
from shapely.geometry import LineString, box, shape
from shapely.ops import polygonize_full, unary_union

from .data import utc_now, write_json
from .graph import clean_properties, uid, validate_graph


def district_boundary(document, division_code):
    matches = [
        row
        for row in document.get("elements", [])
        if row.get("type") == "relation"
        and row.get("tags", {}).get("division_code") == str(division_code)
        and row.get("tags", {}).get("boundary") == "administrative"
    ]
    if len(matches) != 1:
        raise ValueError("区界查询未唯一匹配指定 division_code，拒绝使用其他边界。")
    relation = matches[0]
    rings = {"outer": [], "inner": []}
    for member in relation.get("members", []):
        if member.get("type") != "way":
            continue
        role = member.get("role", "") or "outer"
        if role not in rings:
            continue
        coordinates = [(p["lon"], p["lat"]) for p in member.get("geometry", [])]
        if len(coordinates) < 2:
            raise ValueError("区界成员缺少完整坐标。")
        rings[role].append(LineString(coordinates))
    geometry = None
    for role in ("outer", "inner"):
        if not rings[role]:
            continue
        polygons, cuts, dangles, invalid = polygonize_full(unary_union(rings[role]))
        if any(not part.is_empty for part in (cuts, dangles, invalid)):
            raise ValueError("区界线段未闭合或存在无效环，拒绝自动补线。")
        combined = unary_union(polygons)
        geometry = combined if role == "outer" else geometry.difference(combined)
    if geometry is None or geometry.is_empty or not geometry.is_valid:
        raise ValueError("区界不是有效面。")
    return relation, geometry


def _coordinates(value):
    if value and isinstance(value[0], (int, float)):
        yield value
    else:
        for child in value:
            yield from _coordinates(child)


def _bbox_intersects(geometry, bounds):
    xmin, ymin, xmax, ymax = math.inf, math.inf, -math.inf, -math.inf
    for coordinate in _coordinates(geometry.get("coordinates", [])):
        x, y = coordinate[:2]
        xmin, ymin, xmax, ymax = min(xmin, x), min(ymin, y), max(xmax, x), max(ymax, y)
    west, south, east, north = bounds
    return xmin <= east and xmax >= west and ymin <= north and ymax >= south


def polygon_key(properties):
    # Exactly the official join rule in produce_lod1.py, not OSM nearest-neighbor matching.
    return "".join(str(properties[field]) for field in ("source", "id", "region"))


def stream_polygons(path, boundary_3857, tile, polygon_license, progress=print):
    rows = []
    total = repaired = excluded = 0
    bounds = boundary_3857.bounds
    last = time.monotonic()
    with Path(path).open("rb") as handle:
        for feature in ijson.items(handle, "features.item", use_float=True):
            total += 1
            if time.monotonic() - last >= 20:
                progress(f"{Path(path).name}: scanned {total:,}, retained {len(rows):,}")
                last = time.monotonic()
            geometry = feature.get("geometry") or {}
            if not _bbox_intersects(geometry, bounds):
                continue
            geometry = shape(geometry)
            if not geometry.is_valid:
                repaired += 1
                geometry = make_valid(geometry)
            if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
                excluded += 1
                continue
            if geometry.is_empty or not geometry.intersects(boundary_3857):
                continue
            p = feature["properties"]
            rows.append(
                {
                    "id": "gba:" + ":".join(str(p[k]) for k in ("source", "region", "id")),
                    "gba_join_key": polygon_key(p),
                    "polygon_source": str(p["source"]),
                    "source_polygon_id": str(p["id"]),
                    "region": str(p["region"]),
                    "source_tile": tile,
                    "polygon_license": polygon_license,
                    "geometry": geometry,
                }
            )
    progress(f"{Path(path).name}: scanned {total:,}, retained {len(rows):,}")
    return rows, {"scanned": total, "candidate_repaired": repaired, "excluded_nonpolygon": excluded}


def stream_heights(path, keys, progress=print):
    found = {}
    scanned = 0
    last = time.monotonic()
    with Path(path).open("rb") as handle:
        for key, value in ijson.kvitems(handle, "", use_float=True):
            scanned += 1
            if key in keys:
                if key in found:
                    raise ValueError(f"GBA 高度文件含重复键：{key}")
                found[key] = value
            if time.monotonic() - last >= 20:
                progress(f"Height entries: scanned {scanned:,}, matched {len(found):,}")
                last = time.monotonic()
    progress(f"Height entries: scanned {scanned:,}, matched {len(found):,}")
    return found, scanned


def export_building_graph(buildings, area, summary, destination):
    destination.mkdir(parents=True, exist_ok=True)
    area_id = summary["study_area_id"]
    area_uid = uid(area_id, "StudyArea", area_id)
    nodes, relationships = [], []
    for label, frame in [("StudyArea", area), ("Building", buildings)]:
        centers = frame.geometry.centroid.to_crs(4326)
        for index, row in frame.iterrows():
            identifier = uid(area_id, label, str(row.id))
            properties = row.drop(labels=[frame.geometry.name]).to_dict()
            properties.update(
                source_id=row.id,
                study_area_id=area_id,
                source="GlobalBuildingAtlas" if label == "Building" else "OpenStreetMap",
                geometry_wkt=row.geometry.wkt,
                geometry_crs=frame.crs.to_string(),
                longitude=centers.loc[index].x,
                latitude=centers.loc[index].y,
            )
            nodes.append(
                {"uid": identifier, "label": label, "properties": clean_properties(properties)}
            )
            if label == "Building":
                relationships.append(
                    {
                        "uid": f"{identifier}|PART_OF_DATASET|{area_uid}|",
                        "type": "PART_OF_DATASET",
                        "source": identifier,
                        "target": area_uid,
                        "properties": {"study_area_id": area_id},
                    }
                )
    for filename, records in [("nodes.jsonl", nodes), ("relationships.jsonl", relationships)]:
        with (destination / filename).open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
    write_json(
        destination / "metadata.json",
        {
            "schema_version": 1,
            "study_area_id": area_id,
            "exported_at_utc": utc_now(),
            "nodes": len(nodes),
            "relationships": len(relationships),
            "metric_crs": buildings.crs.to_string(),
            "source_manifest": summary["source_manifest"],
            "semantics": "GBA buildings and dataset membership only; no street/access relations.",
        },
    )
    validate_graph(destination)


def process_district(config, verified_files, progress=print):
    root = config["data_dir"]
    root.mkdir(parents=True, exist_ok=True)
    document = json.loads(config["boundary_file"].read_text(encoding="utf-8"))
    relation, boundary = district_boundary(document, config["division_code"])
    selection_bbox = config.get("selection_bbox")
    if selection_bbox:
        boundary = boundary.intersection(box(*selection_bbox))
        if boundary.is_empty or boundary.area == 0:
            raise ValueError("选取范围与指定区界没有面积交集。")
    area = gpd.GeoDataFrame(
        [{"id": config["id"], "name": config["name"], "osm_relation_id": relation["id"]}],
        geometry=[boundary],
        crs=4326,
    )
    boundary_3857 = area.to_crs(3857).geometry.iloc[0]
    files = {row["kind"]: row for row in verified_files}
    polygon_parts = config.get("polygon_parts", ["odbl", "polygon"])
    partial = set(polygon_parts) != {"odbl", "polygon"}
    rows, counts = [], {}
    for kind, license_name in [("odbl", "ODbL-1.0"), ("polygon", "CC-BY-NC-4.0")]:
        if kind not in polygon_parts:
            continue
        selected, report = stream_polygons(
            files[kind]["local_path"], boundary_3857, config["tile"], license_name, progress
        )
        rows.extend(selected)
        counts[kind] = report
    if not rows:
        raise ValueError("区界内没有 GBA 建筑；检查坐标系和分块。")
    keys = {row["gba_join_key"] for row in rows}
    if len(keys) != len(rows):
        raise ValueError("GBA 建筑存在重复来源键，拒绝静默合并。")
    heights, scanned = stream_heights(files["height"]["local_path"], keys, progress)
    for row in rows:
        value = heights.get(row["gba_join_key"], {})
        height = value.get("height")
        valid = isinstance(height, (int, float)) and math.isfinite(height) and height > 0
        row.update(
            height_m=float(height) if valid else None,
            gba_height_raw=height,
            gba_var_raw=value.get("var"),
            height_record_matched=row["gba_join_key"] in heights,
            height_source="GlobalBuildingAtlas.LoD1",
            height_method="machine_learning_estimate",
            height_license="CC-BY-NC-4.0",
        )
    # Publisher states the polygons are EPSG:3857, even if a file incorrectly says 4326.
    buildings = gpd.GeoDataFrame(rows, crs=3857).to_crs(config["metric_crs"])
    area = area.to_crs(buildings.crs)
    aoi = area.geometry.iloc[0]
    buildings["boundary_intersects"] = ~buildings.geometry.covered_by(aoi)
    buildings["centroid_within_area"] = buildings.geometry.centroid.covered_by(aoi)
    buildings["area_m2"] = buildings.area
    buildings["area_in_district_m2"] = buildings.geometry.intersection(aoi).area
    buildings = buildings.loc[buildings.area_in_district_m2 > 0].reset_index(drop=True)
    buildings["estimated_volume_in_district_m3"] = (
        buildings.area_in_district_m2 * buildings.height_m
    )
    for directory in [root / "raw", root / "processed"]:
        directory.mkdir(exist_ok=True)
    area.to_parquet(root / "raw/area.parquet", index=False)
    area.to_crs(4326).to_file(root / "raw/area.geojson", driver="GeoJSON")
    buildings.to_parquet(root / "processed/buildings.parquet", index=False)
    area.to_parquet(root / "processed/area.parquet", index=False)
    buildings.to_crs(4326).to_file(root / "processed/buildings.geojson", driver="GeoJSON")
    buildings.to_file(root / "processed/buildings.gpkg", layer="buildings", driver="GPKG")
    buildings.drop(columns="geometry").to_csv(
        root / "processed/building_heights.csv", index=False, encoding="utf-8-sig"
    )
    valid_heights = buildings.height_m.dropna()
    provenance = {
        "source": "GlobalBuildingAtlas",
        "publication_doi": "10.5194/essd-17-6647-2025",
        "acquired_at_utc": utc_now(),
        "tile": config["tile"],
        "polygon_parts_used": polygon_parts,
        "partial_building_sample": partial,
        "selection_bbox_wgs84": selection_bbox,
        "download_records": verified_files,
        "polygon_crs_override": "EPSG:3857 according to the publisher README",
        "boundary_source": "OpenStreetMap relation " + str(relation["id"]),
        "boundary_osm_timestamp": document.get("osm3s", {}).get("timestamp_osm_base"),
        "polygon_licenses": [
            license_name
            for kind, license_name in [("odbl", "ODbL-1.0"), ("polygon", "CC-BY-NC-4.0")]
            if kind in polygon_parts
        ],
        "height_license": "CC-BY-NC-4.0",
        "attribution": "Zhu, Chen, Zhang, Shi & Wang (2025); © OpenStreetMap contributors",
        "reference_date_note": "Download date is not the observation/construction date.",
        "software": {name: version(name) for name in ("geopandas", "ijson", "shapely", "pyproj")},
    }
    summary = {
        "study_area_id": config["id"],
        "name": config["name"],
        "processed_at_utc": utc_now(),
        "metric_crs": buildings.crs.to_string(),
        "area_km2": float(aoi.area / 1e6),
        "building_count": len(buildings),
        "partial_building_sample": partial,
        "valid_height_count": int(buildings.height_m.notna().sum()),
        "valid_height_definition": "finite predicted height > 0 m; not survey validation",
        "height_at_least_1m_count": int((buildings.height_m >= 1).sum()),
        "height_coverage_ratio": float(buildings.height_m.notna().mean()),
        "missing_height_count": int(buildings.height_m.isna().sum()),
        "height_record_unmatched_count": int((~buildings.height_record_matched).sum()),
        "boundary_building_count": int(buildings.boundary_intersects.sum()),
        "centroid_within_area_count": int(buildings.centroid_within_area.sum()),
        "polygon_source_counts": {
            str(k): int(v) for k, v in buildings.polygon_source.value_counts().items()
        },
        "height_quantiles_m": {
            str(q): float(v)
            for q, v in valid_heights.quantile([0, 0.25, 0.5, 0.75, 0.95, 1]).items()
        },
        "height_mean_m": float(valid_heights.mean()) if len(valid_heights) else None,
        "scanned": {"polygons": counts, "height_entries": scanned},
        "source_manifest": provenance,
        "semantics": "Predicted heights; full footprints with positive district overlap.",
        "warnings": [
            "OSM boundary is an analysis boundary, not an official cadastral/survey boundary.",
            "GBA heights are model estimates, not surveyed ground truth.",
            "gba_var_raw is preserved; it is not treated as measured error/confidence bounds.",
            "This run did not acquire OSM streets or geometric-neighbor relations.",
        ],
    }
    if not len(valid_heights):
        summary["height_quantiles_m"] = {}
    if partial:
        summary["warnings"].append(
            "Only selected GBA polygon source parts were processed; building counts are partial."
        )
    write_json(root / "source/manifest.json", provenance)
    write_json(root / "processed/summary.json", summary)
    export_building_graph(buildings, area, summary, root / "graph")
    return summary
