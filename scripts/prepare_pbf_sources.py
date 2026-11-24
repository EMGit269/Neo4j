"""Prepare bounded, reference-complete OSMnx inputs from an official regional PBF."""

import argparse
import hashlib
import json
import re
from pathlib import Path

import geopandas as gpd
import osmium
import osmnx as ox
import pandas as pd
import yaml
from shapely.geometry import LineString, Point

from urban_fabric.cleaning import file_sha256
from urban_fabric.config import load_config
from urban_fabric.data import utc_now, write_json

FEATURE_TAGS = {
    "building",
    "building:part",
    "barrier",
    "entrance",
    "landuse",
    "leisure",
    "natural",
    "railway",
    "waterway",
    "amenity",
}
TAGS = FEATURE_TAGS | {"highway"}


def matches_osmnx_filter(tags, expression):
    """Implement exactly OSMnx's presence/negative-regex walk preset, fail on new syntax."""
    clauses = re.findall(r'\["([^"\]]+)"(?:!~"([^"\]]*)")?\]', expression)
    rebuilt = "".join(
        f'["{key}"!~"{pattern}"]' if pattern else f'["{key}"]' for key, pattern in clauses
    )
    if rebuilt != expression:
        raise ValueError("Unsupported OSMnx filter syntax; review the installed walk preset.")
    return all(
        re.search(pattern, str(tags.get(key, ""))) is None if pattern else key in tags
        for key, pattern in clauses
    )


class AreaSelector(osmium.SimpleHandler):
    def __init__(self, writer, polygon):
        super().__init__()
        self.writer, self.polygon = writer, polygon
        self.bounds = polygon.bounds
        self.ways = set()
        self.counts = {"selected_nodes": 0, "selected_ways": 0, "selected_relations": 0}

    def node(self, node):
        if any(key in node.tags for key in TAGS) and node.location.valid():
            if self.polygon.covers(Point(node.location.lon, node.location.lat)):
                self.writer.add(node)
                self.counts["selected_nodes"] += 1

    def way(self, way):
        coordinates = [(n.lon, n.lat) for n in way.nodes if n.location.valid()]
        if len(coordinates) < 2 or len(coordinates) != len(way.nodes):
            return
        west, south, east, north = self.bounds
        xs, ys = zip(*coordinates, strict=True)
        if max(xs) < west or min(xs) > east or max(ys) < south or min(ys) > north:
            return
        line = LineString(coordinates)
        # Closed building shells may contain the buffer without their outline touching it.
        intersects = line.intersects(self.polygon)
        if not intersects and line.is_ring:
            from shapely.geometry import Polygon

            intersects = Polygon(line).intersects(self.polygon)
        if intersects:
            self.ways.add(way.id)
            if any(key in way.tags for key in TAGS):
                self.writer.add(way)
                self.counts["selected_ways"] += 1

    def relation(self, relation):
        if any(key in relation.tags for key in TAGS) and any(
            m.type == "w" and m.ref in self.ways for m in relation.members
        ):
            self.writer.add(relation)
            self.counts["selected_relations"] += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--pbf", type=Path, required=True)
    parser.add_argument("--source-url", required=True)
    args = parser.parse_args()
    checksum_file = Path(str(args.pbf) + ".md5")
    checksum_verified = False
    if checksum_file.exists():
        expected = checksum_file.read_text().split()[0]
        if hashlib.md5(args.pbf.read_bytes()).hexdigest() != expected:
            raise ValueError("Regional PBF does not match the publisher checksum.")
        checksum_verified = True
    settings = load_config(args.config)
    doc = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    source = settings.data_dir / "source"
    source.mkdir(parents=True, exist_ok=True)
    area = gpd.read_file(settings.polygon_path).to_crs(settings.metric_crs)
    area["id"] = settings.study_area_id
    context = gpd.GeoDataFrame(
        geometry=area.buffer(doc["cleaning"]["context_buffer_m"]), crs=area.crs
    )
    area.to_parquet(source / "area.parquet", index=False)
    context.to_parquet(source / "context.parquet", index=False)
    polygon = context.to_crs(4326).geometry.union_all()
    subset = source / "osm_context.osm"
    if not subset.exists():
        with osmium.BackReferenceWriter(
            subset, args.pbf, remove_tags=False, relation_depth=2
        ) as out:
            selector = AreaSelector(out, polygon)
            selector.apply_file(args.pbf, locations=True, idx="flex_mem")
        print(selector.counts, flush=True)
    # OSMnx's public reader fixes geometry internally. Inspect its assembly stage first
    # so these upstream changes are also visible rather than reporting zero repairs.
    assembled = ox.features._process_features(
        ox._osm_xml._overpass_json_from_xml(subset, "utf-8")["elements"], FEATURE_TAGS
    )
    upstream_events = []
    for feature in assembled:
        geom = feature["geometry"]
        if geom is None or geom.is_empty or not geom.is_valid:
            upstream_events.append(
                {
                    "id": f"osm:{feature['element']}:{feature['id']}",
                    "action": "OSMnx_make_valid_or_empty_filter",
                    "geometry_wkt_before": geom.wkt if geom is not None else None,
                }
            )
    write_json(
        source / "osm_feature_geometry_audit.json",
        {
            "note": "Pre-filter OSMnx assembly; uses version-pinned internal API for audit only.",
            "events": upstream_events,
        },
    )
    features = ox.features_from_xml(
        subset,
        polygon=polygon,
        tags={tag: True for tag in FEATURE_TAGS},
    ).reset_index()
    repaired_ids = {event["id"] for event in upstream_events}
    features["osmnx_geometry_repaired"] = [
        f"osm:{row.element}:{row.id}" in repaired_ids for row in features.itertuples()
    ]
    for column in features.columns:
        if column == features.geometry.name:
            continue

        def normalize(value):
            if isinstance(value, (dict, list, tuple)):
                return json.dumps(value, ensure_ascii=False)
            return None if pd.isna(value) else str(value)

        features[column] = features[column].map(normalize)
    features.to_parquet(source / "osm_features.parquet", index=False)
    # Reuse the installed OSMnx filter verbatim rather than approximate legal walkability.
    expression = ox._overpass._get_network_filter("walk")
    roads = source / "osm_walk_context.osm"
    with osmium.BackReferenceWriter(roads, subset, remove_tags=False, overwrite=True) as writer:
        for entity in osmium.FileProcessor(subset, osmium.osm.WAY):
            if matches_osmnx_filter(entity.tags, expression):
                writer.add(entity)
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
    graph = ox.graph_from_xml(roads, bidirectional=True, simplify=False, retain_all=True)
    graph = ox.truncate.truncate_graph_polygon(graph, polygon, truncate_by_edge=True)
    ox.save_graphml(graph, source / "osm_unsimplified.graphml")
    with osmium.io.Reader(args.pbf) as reader:
        header = reader.header()
        timestamp = header.get("osmosis_replication_timestamp")
    if not timestamp:
        state = args.pbf.parent / (args.pbf.name.removesuffix(".osm.pbf") + ".state.txt")
        if state.exists():
            timestamp = next(
                (
                    line.split("=", 1)[1].replace("\\:", ":")
                    for line in state.read_text().splitlines()
                    if line.startswith("timestamp=")
                ),
                "",
            )
    manifest = {
        "source": "OpenStreetMap France regional extract",
        "source_url": args.source_url,
        "license": "ODbL-1.0",
        "attribution": "© OpenStreetMap contributors",
        "acquired_at_utc": utc_now(),
        "snapshot_timestamp_utc": timestamp,
        "pbf_sha256": file_sha256(args.pbf),
        "publisher_md5_verified": checksum_verified,
        "subset_sha256": file_sha256(subset),
        "context_buffer_m": doc["cleaning"]["context_buffer_m"],
        "query_bounds": list(polygon.bounds),
        "osmnx_walk_filter": expression,
        "counts": {
            "features": len(features),
            "nodes": len(graph),
            "directed_edges": graph.number_of_edges(),
        },
        "semantics": "Mapped geometry and tags; coverage and public access remain unverified.",
    }
    for part in ["features", "network"]:
        write_json(source / f"{part}_acquisition.json", {**manifest, "part": part})
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
