"""Retrieve the selected window's broad OSM place/feature inventory with OSMnx."""

import argparse
import hashlib
import json
from pathlib import Path

import geopandas as gpd
import osmnx as ox
import pandas as pd
import yaml
from shapely.geometry import LineString, Point, Polygon

from urban_fabric.data import utc_now, write_json


def matches_tags(tags, query):
    return any(
        key in tags
        and (value is True or tags[key] in (value if isinstance(value, list) else [value]))
        for key, value in query.items()
    )


def audit_unassembled(source, manifest):
    """Preserve unsupported relation semantics without inventing a POI centroid."""
    selection = source / "pbf_selection_ids.json"
    if not selection.exists():
        return
    import osmium

    features = gpd.read_parquet(source / "osm_features_wgs84.parquet")
    absent = set(json.loads(selection.read_text())) - set(features.osm_uid)
    write_json(source / "unassembled_selected_ids.json", sorted(absent))
    rows = []
    for item in osmium.FileProcessor(source / "osm_poi_window.osm", osmium.osm.RELATION):
        uid = f"osm:relation:{item.id}"
        if uid not in absent:
            continue
        tags = dict(item.tags)
        grouping = tags.get("public_transport") in {"stop_area", "stop_area_group"}
        rows.append(
            {
                "osm_uid": uid,
                "name": tags.get("name:zh") or tags.get("name", ""),
                "relation_type": tags.get("type", ""),
                "decision": "context_group" if grouping or tags.get("type") == "site" else "review",
                "reason_zh": "交通分组关系"
                if grouping
                else "文化遗产场所集合"
                if tags.get("heritage")
                else "未组装关系对象",
                "tags_json": json.dumps(tags, ensure_ascii=False),
                "members_json": json.dumps(
                    [{"type": m.type, "ref": m.ref, "role": m.role} for m in item.members],
                    ensure_ascii=False,
                ),
            }
        )
    write_json(source / "unassembled_relations.json", rows)
    pd.DataFrame(rows).to_csv(source / "unassembled_relations.csv", index=False)
    manifest["selected_objects_not_assembled_by_osmnx"] = len(absent)
    manifest["unassembled_relation_decisions"] = (
        pd.Series([r["decision"] for r in rows]).value_counts().to_dict()
    )
    write_json(source / "acquisition.json", manifest)


def read_pbf(path, polygon, query, source):
    import osmium

    class Selector(osmium.SimpleHandler):
        def __init__(self, writer):
            super().__init__()
            self.writer = writer
            self.nodes_in_area, self.ways_in_area = set(), set()
            self.selected = set()
            self.bounds = polygon.bounds

        def node(self, node):
            if not node.location.valid():
                return
            lon, lat = node.location.lon, node.location.lat
            west, south, east, north = self.bounds
            if west <= lon <= east and south <= lat <= north and polygon.covers(Point(lon, lat)):
                self.nodes_in_area.add(node.id)
                if matches_tags(node.tags, query):
                    self.writer.add(node)
                    self.selected.add(f"osm:node:{node.id}")

        def way(self, way):
            coords = [(n.lon, n.lat) for n in way.nodes if n.location.valid()]
            if len(coords) != len(way.nodes) or len(coords) < 2:
                return
            west, south, east, north = polygon.bounds
            xs, ys = zip(*coords, strict=True)
            if max(xs) < west or min(xs) > east or max(ys) < south or min(ys) > north:
                return
            geometry = LineString(coords)
            if not geometry.intersects(polygon):
                if coords[0] != coords[-1] or len(coords) < 4:
                    return
                if not Polygon(coords).intersects(polygon):
                    return
            self.ways_in_area.add(way.id)
            if matches_tags(way.tags, query):
                self.writer.add(way)
                self.selected.add(f"osm:way:{way.id}")

        def relation(self, relation):
            if matches_tags(relation.tags, query) and any(
                (m.type == "w" and m.ref in self.ways_in_area)
                or (m.type == "n" and m.ref in self.nodes_in_area)
                for m in relation.members
            ):
                self.writer.add(relation)
                self.selected.add(f"osm:relation:{relation.id}")

    subset = source / "osm_poi_window.osm"
    if not subset.exists():
        with osmium.BackReferenceWriter(
            subset, path, remove_tags=False, relation_depth=4
        ) as writer:
            handler = Selector(writer)
            handler.apply_file(path, locations=True, idx="flex_mem")
        write_json(source / "pbf_selection_ids.json", sorted(handler.selected))
        print("PBF selected objects:", len(handler.selected), flush=True)
    features = ox.features_from_xml(subset, polygon=polygon, tags=query).reset_index()
    with osmium.io.Reader(path) as reader:
        timestamp = reader.header().get("osmosis_replication_timestamp")
    state_file = path.parent / (path.name.removesuffix(".osm.pbf") + ".state.txt")
    if not timestamp and state_file.exists():
        timestamp = next(
            (
                line.split("=", 1)[1].replace("\\:", ":")
                for line in state_file.read_text().splitlines()
                if line.startswith("timestamp=")
            ),
            None,
        )
    publisher_path = path.with_suffix(".acquisition.json")
    publisher = json.loads(publisher_path.read_text()) if publisher_path.exists() else {}
    return features, {
        "source": "OpenStreetMap France Jiangsu PBF, locally extracted with PyOsmium/OSMnx",
        "pbf_path": str(path.resolve()),
        "pbf_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "snapshot_timestamp_utc": timestamp or None,
        "publisher_download": publisher,
        "subset_sha256": hashlib.sha256(subset.read_bytes()).hexdigest(),
        "selection_note": (
            "Nodes/ways intersecting the AOI and tagged relations with member "
            "nodes/ways in it; full references retained."
        ),
    }


def main(args):
    path = args.config.resolve()
    rules = yaml.safe_load(path.read_text())
    area_file = (path.parent / rules["area_file"]).resolve()
    source = (path.parent / rules["output_dir"]).resolve() / "source"
    source.mkdir(parents=True, exist_ok=True)
    area = gpd.read_file(area_file).to_crs(4326)
    polygon = area.geometry.union_all()
    request_spec = {
        "study_area": rules["study_area"],
        "bounds_wsen": list(polygon.bounds),
        "area_file_sha256": hashlib.sha256(area_file.read_bytes()).hexdigest(),
        "tags_union": rules["query_tags"],
    }
    target = source / "osm_features_wgs84.parquet"
    manifest_path = source / "acquisition.json"
    if target.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest["request_spec"] != request_spec:
            raise ValueError(
                "Existing source query differs. Preserve it and use a new output directory."
            )
        audit_unassembled(source, manifest)
        print("Existing acquisition reused:", target, flush=True)
        return
    ox.settings.cache_folder = source / "cache"
    ox.settings.use_cache = True
    ox.settings.log_console = True
    ox.settings.requests_timeout = args.timeout
    ox.settings.overpass_url = args.overpass_url.rstrip("/")
    ox.settings.http_user_agent = "urban-fabric-lab/0.1 (Suzhou academic POI inventory; OSMnx)"
    write_json(source / "query_spec.json", {**request_spec, "started_at_utc": utc_now()})
    provenance = {}
    if args.pbf:
        features, provenance = read_pbf(args.pbf, polygon, rules["query_tags"], source)
    else:
        features = ox.features_from_polygon(polygon, tags=rules["query_tags"]).reset_index()
    features = features.rename(columns={"element": "osm_type", "id": "osm_id"})
    features["osm_uid"] = "osm:" + features.osm_type + ":" + features.osm_id.astype(str)
    # Keep heterogeneous OSM tag values losslessly as text/JSON, with typed WGS84 geometry.
    for column in features.columns:
        if column == features.geometry.name:
            continue

        def normalize(value):
            if isinstance(value, (dict, list, tuple)):
                return json.dumps(value, ensure_ascii=False)
            return None if pd.isna(value) else str(value)

        features[column] = features[column].map(normalize)
    if not features.osm_uid.is_unique:
        raise ValueError("OSM element identity duplicated after assembly.")
    features.to_parquet(target, index=False)
    cache = []
    for item in sorted((source / "cache").glob("*.json")):
        payload = json.loads(item.read_text())
        if "elements" not in payload:
            continue
        if payload.get("remark"):
            raise ValueError("Overpass returned a remark; completeness requires inspection.")
        cache.append(
            {
                "file": item.name,
                "osm3s": payload.get("osm3s"),
                "elements": len(payload["elements"]),
                "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
            }
        )
    manifest = {
        "source": "OpenStreetMap via OSMnx Overpass",
        "acquired_at_utc": utc_now(),
        "request_spec": request_spec,
        "osmnx_version": ox.__version__,
        "overpass_url": None if args.pbf else ox.settings.overpass_url,
        "assembled_features": len(features),
        "geometry_types": features.geom_type.value_counts().to_dict(),
        "element_types": features.osm_type.value_counts().to_dict(),
        "raw_cache": cache,
        "source_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "license": "ODbL-1.0",
        "attribution": "© OpenStreetMap contributors",
        "scope": "No sample cap; all returned features matching the declared union of tag filters.",
        "limits": (
            "OSM mapping coverage and tags are incomplete; this is not every real-world place."
        ),
        **provenance,
    }
    write_json(manifest_path, manifest)
    audit_unassembled(source, manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/osm-poi-filter.yaml"))
    parser.add_argument("--overpass-url", default="https://overpass.private.coffee/api")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--pbf", type=Path, help="Read a regional PBF instead of online Overpass.")
    main(parser.parse_args())
