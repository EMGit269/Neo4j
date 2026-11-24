"""Audited OSM/GBA preparation. OSM node identity, not XY crossings, defines topology."""

import hashlib
import json
from collections import Counter
from pathlib import Path

import geopandas as gpd
import momepy
import networkx as nx
import numpy as np
import osmnx as ox
import pandas as pd
import shapely
import yaml
from shapely.geometry import Point

from .config import load_config
from .data import Layers, utc_now, write_json
from .fetch import _tag
from .graph import export_graph, validate_graph
from .metrics import _polygon, analyze, parse_height

SEMANTIC_NODE_TAGS = ["barrier", "entrance", "crossing", "access", "foot"]
SEMANTIC_EDGE_TAGS = [
    "bridge",
    "tunnel",
    "layer",
    "access",
    "foot",
    "service",
    "highway",
    "foot:conditional",
    "access:conditional",
    "oneway:foot",
    "area",
]


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _records(path, rows):
    write_json(path, rows)


def clean_buildings(features, boundary, gba, *, id_iou=0.70, spatial_iou=0.90):
    """Repair/retain footprints; transfer height only through unambiguous geometry matches."""
    rows, events, aliases = [], [], []
    for _, original in features.iterrows():
        tag = original.get("building")
        if pd.isna(tag) or str(tag) in {"no", "False", "None"}:
            continue
        source_id = str(original.get("id", original.get("osmid")))
        element = str(original.get("element", "way"))
        identifier = f"osm:{element}:{source_id}"
        geom = original.geometry
        cleaned = _polygon(geom)
        if cleaned is None or cleaned.is_empty or cleaned.area <= 0:
            events.append({"id": identifier, "action": "quarantine_nonpolygon_or_empty"})
            continue
        if str(tag) in {"proposed", "demolished", "razed", "destroyed"}:
            events.append({"id": identifier, "action": "quarantine_nonexisting_building_tag"})
            continue
        if not cleaned.intersects(boundary):
            continue
        repaired = not geom.is_valid or geom.geom_type != cleaned.geom_type
        upstream_repaired = str(original.get("osmnx_geometry_repaired", "false")).lower() == "true"
        if repaired:
            events.append(
                {
                    "id": identifier,
                    "action": "make_valid_polygon_parts",
                    "original_type": geom.geom_type,
                    "result_type": cleaned.geom_type,
                    "area_before_m2": float(geom.area),
                    "area_after_m2": cleaned.area,
                }
            )
        row = original.drop(labels=[features.geometry.name]).to_dict()
        row.update(
            id=identifier,
            osm_id=source_id,
            osm_element=element,
            geometry=cleaned,
            source="OpenStreetMap",
            geometry_repaired=repaired or upstream_repaired,
            building_levels=original.get("building:levels"),
            within_core=boundary.covers(cleaned.centroid),
        )
        rows.append(row)
    buildings = gpd.GeoDataFrame(rows, crs=features.crs)
    if not len(buildings):
        raise ValueError("No valid building footprints in the core study area.")
    if buildings.id.duplicated().any():
        raise ValueError("Duplicate OSM element IDs in feature source.")
    # Exact normalized geometry only. Keep discarded attributes in the alias ledger.
    buildings["_fingerprint"] = buildings.geometry.map(
        lambda g: shapely.to_wkb(shapely.normalize(g), hex=True)
    )
    buildings["_relation"] = buildings.osm_element.eq("relation")
    buildings["height_tag_source_id"] = buildings.id
    buildings["alias_height_conflict"] = False
    buildings = buildings.sort_values(["_relation", "id"], ascending=[False, True])
    keep = []
    for _, group in buildings.groupby("_fingerprint", sort=False):
        keep.append(group.index[0])
        heights = [(i, parse_height(row.get("height"))) for i, row in group.iterrows()]
        valid_heights = [(i, height) for i, height in heights if height is not None]
        unique_heights = {height for _, height in valid_heights}
        if len(unique_heights) > 1:
            buildings.loc[group.index[0], "alias_height_conflict"] = True
        elif valid_heights and parse_height(group.iloc[0].get("height")) is None:
            alias_index = valid_heights[0][0]
            buildings.loc[group.index[0], "height"] = buildings.loc[alias_index, "height"]
            buildings.loc[group.index[0], "height_tag_source_id"] = buildings.loc[alias_index, "id"]
        for _, duplicate in group.iloc[1:].iterrows():
            aliases.append(
                {
                    "kept_id": group.id.iloc[0],
                    "alias_id": duplicate.id,
                    "action": "exact_geometry_alias",
                    "original_tags": {
                        str(k): _tag(v)
                        for k, v in duplicate.items()
                        if k not in {"geometry", "_fingerprint"}
                    },
                }
            )
    buildings = buildings.loc[keep].drop(columns=["_fingerprint", "_relation"])
    buildings = buildings.sort_values("id").reset_index(drop=True)
    buildings["osm_height_m"] = buildings.get(
        "height", pd.Series(index=buildings.index, dtype=object)
    ).map(parse_height)
    buildings["gba_height_m"] = np.nan
    buildings["gba_match_iou"] = np.nan
    buildings["gba_match_id"] = None
    buildings["gba_match_method"] = None
    buildings["gba_height_license"] = None
    buildings["gba_height_method"] = None
    buildings["gba_height_var_raw"] = np.nan
    buildings["height_source"] = None
    buildings["height_conflict"] = False
    candidates, ambiguous, matched_gba = [], [], set()
    if len(gba):
        gba = gba.to_crs(features.crs).reset_index(drop=True)
        for bi, gi in zip(
            *gba.sindex.query(buildings.geometry, predicate="intersects"), strict=True
        ):
            b, g = buildings.iloc[bi], gba.iloc[gi]
            intersection = b.geometry.intersection(g.geometry).area
            union = b.geometry.union(g.geometry).area
            iou = intersection / union if union else 0
            same_id = str(g.get("polygon_source")) == "osm" and b.osm_id == str(
                g.get("source_polygon_id")
            )
            threshold = id_iou if same_id else spatial_iou
            if iou >= threshold:
                candidates.append(
                    {
                        "bi": int(bi),
                        "gi": int(gi),
                        "iou": float(iou),
                        "method": "source_id_and_iou" if same_id else "spatial_iou",
                    }
                )
        by_building = Counter(c["bi"] for c in candidates)
        by_gba = Counter(c["gi"] for c in candidates)
        for candidate in candidates:
            bi, gi = candidate["bi"], candidate["gi"]
            if by_building[bi] != 1 or by_gba[gi] != 1:
                ambiguous.append(
                    {**candidate, "building_id": buildings.id.iloc[bi], "gba_id": gba.id.iloc[gi]}
                )
                continue
            height = pd.to_numeric(gba.iloc[gi].get("height_m"), errors="coerce")
            if pd.isna(height) or not np.isfinite(height) or height <= 0:
                continue
            matched_gba.add(gi)
            buildings.loc[bi, ["gba_height_m", "gba_match_iou"]] = height, candidate["iou"]
            buildings.loc[bi, "gba_match_id"] = str(gba.id.iloc[gi])
            buildings.loc[bi, "gba_match_method"] = candidate["method"]
            buildings.loc[bi, "gba_height_license"] = gba.iloc[gi].get("height_license")
            buildings.loc[bi, "gba_height_method"] = gba.iloc[gi].get("height_method")
            buildings.loc[bi, "gba_height_var_raw"] = pd.to_numeric(
                gba.iloc[gi].get("gba_var_raw"), errors="coerce"
            )
    buildings["height_m"] = pd.to_numeric(buildings.osm_height_m, errors="coerce").fillna(
        buildings.gba_height_m
    )
    buildings.loc[buildings.gba_height_m.notna(), "height_source"] = "GBA predicted height"
    buildings.loc[buildings.osm_height_m.notna(), "height_source"] = "OSM explicit height tag"
    both = buildings.osm_height_m.notna() & buildings.gba_height_m.notna()
    ratio = buildings.osm_height_m / buildings.gba_height_m
    buildings.loc[both & ((ratio > 1.5) | (ratio < 1 / 1.5)), "height_conflict"] = True
    buildings["height_is_survey_verified"] = False
    buildings["height_requires_review"] = (
        buildings.height_conflict | buildings.alias_height_conflict
    )
    buildings["height_for_analysis_m"] = buildings.height_m.where(~buildings.height_requires_review)
    overlaps = []
    buildings["overlap_review"] = False
    for a, b in zip(
        *buildings.sindex.query(buildings.geometry, predicate="intersects"), strict=True
    ):
        if a >= b:
            continue
        ga, gb = buildings.geometry.iloc[a], buildings.geometry.iloc[b]
        size = ga.intersection(gb).area
        if size > max(1.0, 0.02 * min(ga.area, gb.area)):
            overlaps.append(
                {
                    "a": buildings.id.iloc[a],
                    "b": buildings.id.iloc[b],
                    "intersection_m2": float(size),
                    "fraction_of_smaller": float(size / min(ga.area, gb.area)),
                }
            )
            buildings.loc[[a, b], "overlap_review"] = True
    unmatched = (
        gba.loc[~gba.index.isin(matched_gba) & gba.geometry.intersects(boundary)].copy()
        if len(gba)
        else gba.copy()
    )
    buildings["footprint_requires_review"] = buildings.overlap_review | buildings.geometry_repaired
    return (
        buildings,
        {
            "geometry_events": events,
            "exact_geometry_aliases": aliases,
            "ambiguous_height_candidates": ambiguous,
            "substantive_overlaps": overlaps,
            "height_matches": len(matched_gba),
        },
        unmatched,
    )


def orient_line(line, start, end, tolerance=0.02):
    """Orient to explicit endpoints; reject mismatches rather than inventing a link."""
    if line.geom_type != "LineString" or not line.is_valid or line.is_empty:
        raise ValueError("Street segment must be a nonempty valid LineString.")
    a, b = Point(line.coords[0]), Point(line.coords[-1])
    if max(a.distance(start), b.distance(end)) <= tolerance:
        return line, False
    if max(b.distance(start), a.distance(end)) <= tolerance:
        return shapely.reverse(line), True
    raise ValueError("Street geometry endpoints disagree with OSM node identity.")


def clean_network(raw_graph, core, crs, *, endpoint_tolerance=0.02):
    """Simplify without destroying barriers, grade/access transitions, rings or components."""
    if raw_graph.graph.get("simplified", False):
        raise ValueError("Use the unsimplified acquired graph to retain semantic transitions.")
    graph = ox.simplification.simplify_graph(
        raw_graph,
        node_attrs_include=SEMANTIC_NODE_TAGS,
        edge_attrs_differ=SEMANTIC_EDGE_TAGS,
        remove_rings=False,
        track_merged=True,
    )
    graph = ox.project_graph(graph, to_crs=crs)
    physical = ox.convert.to_undirected(graph)
    nodes, edges = ox.graph_to_gdfs(physical)
    node_rows, edge_rows = [], []
    for identifier, node in nodes.iterrows():
        row = {k: _tag(v) for k, v in node.drop(labels=["geometry"]).items()}
        row.update(
            id=f"osm:node:{identifier}",
            osm_id=str(identifier),
            geometry=node.geometry,
            source="OpenStreetMap",
            within_area=core.covers(node.geometry),
        )
        node_rows.append(row)
    reversals = 0
    # Canonical IDs include OSM IDs + geometry + endpoints, preserving genuine parallel ways.
    for (u, v, key), edge in edges.iterrows():
        left, right = sorted([u, v])
        line, reversed_geometry = orient_line(
            edge.geometry, nodes.geometry.loc[left], nodes.geometry.loc[right], endpoint_tolerance
        )
        reversals += reversed_geometry
        osm_ids = edge.get("osmid")
        if isinstance(osm_ids, list):
            osm_ids = sorted(osm_ids, key=str)
        provenance = _tag(osm_ids)
        token = hashlib.sha256(
            f"{left}|{right}|{provenance}|{shapely.normalize(line).wkb_hex}".encode()
        ).hexdigest()[:20]
        row = {k: _tag(val) for k, val in edge.drop(labels=["geometry"]).items()}
        row.update(
            id=f"osm:segment:{left}:{right}:{token}",
            u=f"osm:node:{left}",
            v=f"osm:node:{right}",
            osm_id=provenance,
            source="OpenStreetMap",
            geometry=line,
            length_m=float(line.length),
            length_osmnx_m=float(edge.get("length", 0)),
            intersects_core=line.intersects(core),
            access_verified=False,
            topology_basis="OSM node identity",
        )
        edge_rows.append(row)
    node_frame = gpd.GeoDataFrame(node_rows, crs=nodes.crs)
    edge_frame = gpd.GeoDataFrame(edge_rows, crs=edges.crs).sort_values("id").reset_index(drop=True)
    if edge_frame.id.duplicated().any():
        raise ValueError("Duplicate physical edge identity after reciprocal deduplication.")
    return (
        node_frame,
        edge_frame,
        graph,
        {
            "unsimplified_nodes": len(raw_graph),
            "unsimplified_directed_edges": raw_graph.number_of_edges(),
            "simplified_directed_nodes": len(graph),
            "simplified_directed_edges": graph.number_of_edges(),
            "physical_segments": len(edge_frame),
            "geometry_orientations_corrected": int(reversals),
            "node_attrs_include": SEMANTIC_NODE_TAGS,
            "edge_attrs_differ": SEMANTIC_EDGE_TAGS,
        },
    )


def audit_layers(layers, *, tolerance=0.02, near_miss_m=2):
    """Hard geometry/topology checks plus a non-mutating, explicitly uncertain review queue."""
    errors = []
    for name in ["area", "buildings", "street_nodes", "street_segments"]:
        frame = getattr(layers, name)
        if frame.id.isna().any() or frame.id.duplicated().any():
            errors.append(f"{name}: invalid/duplicate IDs")
        if frame.geometry.isna().any() or frame.geometry.is_empty.any():
            errors.append(f"{name}: null/empty geometry")
        if not frame.geometry.is_valid.all():
            errors.append(f"{name}: invalid geometry")
        if frame.crs != layers.area.crs:
            errors.append(f"{name}: inconsistent CRS")
    nodes = layers.street_nodes.set_index("id")
    edges = layers.street_segments
    graph = nx.MultiGraph()
    graph.add_nodes_from(nodes.index)
    max_endpoint_error = 0
    for row in edges.itertuples():
        if row.u not in nodes.index or row.v not in nodes.index:
            errors.append(f"{row.id}: missing endpoint")
            continue
        if row.geometry.geom_type != "LineString":
            errors.append(f"{row.id}: non-LineString geometry")
            continue
        residual = max(
            Point(row.geometry.coords[0]).distance(nodes.geometry.loc[row.u]),
            Point(row.geometry.coords[-1]).distance(nodes.geometry.loc[row.v]),
        )
        max_endpoint_error = max(max_endpoint_error, residual)
        if residual > tolerance:
            errors.append(f"{row.id}: endpoint mismatch")
        if not np.isfinite(row.length_m) or row.length_m <= 0:
            errors.append(f"{row.id}: invalid metric weight")
        elif not np.isclose(row.length_m, row.geometry.length, rtol=1e-9, atol=tolerance):
            errors.append(f"{row.id}: length/geometry mismatch")
        graph.add_edge(row.u, row.v, key=row.id, length=row.length_m)
    components = sorted(nx.connected_components(graph), key=lambda c: (-len(c), sorted(c)[0]))
    membership = {n: i for i, component in enumerate(components) for n in component}
    near_misses, crossings, coincident_nodes = [], [], []
    # These are review candidates, never automatic snap/connection commands.
    for row in layers.street_nodes.itertuples():
        if graph.degree(row.id) != 1:
            continue
        for i in edges.sindex.query(row.geometry, predicate="dwithin", distance=near_miss_m):
            target = edges.iloc[i]
            if row.id in {target.u, target.v}:
                continue
            near_misses.append(
                {
                    "node_id": row.id,
                    "segment_id": target.id,
                    "distance_m": float(row.geometry.distance(target.geometry)),
                    "different_component": membership[row.id] != membership[target.u],
                    "action": "review_only_no_snap",
                }
            )
    for a, b in zip(*edges.sindex.query(edges.geometry, predicate="crosses"), strict=True):
        if a >= b:
            continue
        ra, rb = edges.iloc[a], edges.iloc[b]
        if {ra.u, ra.v} & {rb.u, rb.v}:
            continue

        def grade_indicated(row):
            for tag in ["layer", "bridge", "tunnel"]:
                value = row.get(tag)
                if value is not None and str(value).lower() not in {
                    "none",
                    "nan",
                    "no",
                    "0",
                    "false",
                    "<na>",
                    "0.0",
                }:
                    return True
            return False

        crossings.append(
            {
                "a": ra.id,
                "b": rb.id,
                "a_layer": _tag(ra.get("layer")),
                "b_layer": _tag(rb.get("layer")),
                "a_bridge": _tag(ra.get("bridge")),
                "b_bridge": _tag(rb.get("bridge")),
                "a_tunnel": _tag(ra.get("tunnel")),
                "b_tunnel": _tag(rb.get("tunnel")),
                "grade_separation_tags_present": grade_indicated(ra) or grade_indicated(rb),
                "action": "retain_OSM_topology_review_crossing",
            }
        )
    node_frame = layers.street_nodes
    for a, b in zip(
        *node_frame.sindex.query(node_frame.geometry, predicate="dwithin", distance=tolerance),
        strict=True,
    ):
        if a < b:
            coincident_nodes.append(
                {
                    "a": node_frame.id.iloc[a],
                    "b": node_frame.id.iloc[b],
                    "distance_m": float(
                        node_frame.geometry.iloc[a].distance(node_frame.geometry.iloc[b])
                    ),
                    "action": "review_no_merge",
                }
            )
    report = {
        "checked_at_utc": utc_now(),
        "hard_checks_passed": not errors,
        "hard_errors": errors,
        "checks": [
            "unique IDs",
            "valid nonempty geometry",
            "shared metric CRS",
            "existing endpoints",
            "oriented endpoints",
            "positive finite weights",
            "geometry-derived weights",
            "preserved multiedges and OSM topology",
        ],
        "maximum_endpoint_residual_m": float(max_endpoint_error),
        "connected_components": len(components),
        "component_sizes": list(map(len, components)),
        "isolated_nodes": len(list(nx.isolates(graph))),
        "self_loops": nx.number_of_selfloops(graph),
        "parallel_node_pairs": sum(
            graph.number_of_edges(u, v) > 1 for u, v in nx.Graph(graph).edges
        ),
        "near_miss_candidates": near_misses,
        "crossings_without_shared_node": crossings,
        "crossings_with_grade_indication": sum(
            row["grade_separation_tags_present"] for row in crossings
        ),
        "coincident_distinct_nodes": coincident_nodes,
        "component_by_node": membership,
    }
    return report


def _save_review_layers(features, boundary, directory):
    for name in ["entrance", "barrier"]:
        if name not in features:
            frame = features.iloc[:0].copy()
        else:
            frame = features[features[name].notna() & features.intersects(boundary)].copy()
        frame.to_parquet(directory / f"osm_{name}s.parquet", index=False)
    if "building:part" in features:
        parts = features[features["building:part"].notna() & features.intersects(boundary)].copy()
        parts.to_parquet(directory / "osm_building_parts.parquet", index=False)
    urban_tags = [
        tag
        for tag in ["landuse", "leisure", "natural", "railway", "waterway", "amenity"]
        if tag in features
    ]
    if urban_tags:
        urban = features[features[urban_tags].notna().any(axis=1)].copy()
        urban["intersects_core"] = urban.intersects(boundary)
        urban.to_parquet(directory / "osm_urban_features.parquet", index=False)


def _save_review_queue(layers, building_audit, report, directory):
    nodes = layers.street_nodes.set_index("id")
    edges = layers.street_segments.set_index("id")
    buildings = layers.buildings.set_index("id")
    rows = []

    def add(kind, first, second, geometry, note):
        rows.append(
            {
                "category": kind,
                "source_id": first,
                "related_id": second,
                "review_status": "unverified",
                "note": note,
                "geometry": geometry.representative_point(),
            }
        )

    for item in report["near_miss_candidates"]:
        add(
            "near_miss",
            item["node_id"],
            item["segment_id"],
            nodes.geometry.loc[item["node_id"]],
            "Do not snap without checking levels/barriers.",
        )
    for item in report["crossings_without_shared_node"]:
        add(
            "crossing_no_shared_node",
            item["a"],
            item["b"],
            edges.geometry.loc[item["a"]].intersection(edges.geometry.loc[item["b"]]),
            "May be a grade separation or mapping gap; no connection fabricated.",
        )
    for item in report["coincident_distinct_nodes"]:
        add(
            "coincident_distinct_osm_nodes",
            item["a"],
            item["b"],
            nodes.geometry.loc[item["a"]],
            "Check topology before merging distinct OSM identities.",
        )
    for item in building_audit["substantive_overlaps"]:
        add(
            "building_overlap",
            item["a"],
            item["b"],
            buildings.geometry.loc[item["a"]].intersection(buildings.geometry.loc[item["b"]]),
            "Review building parts/stacked forms/duplicate mapping before per-building inference.",
        )
    for row in layers.buildings[layers.buildings.height_conflict].itertuples():
        add(
            "height_conflict",
            row.id,
            row.gba_match_id,
            row.geometry,
            "OSM and GBA height differ by more than a factor of 1.5.",
        )
    if not rows:
        frame = gpd.GeoDataFrame(
            {"category": [], "source_id": [], "related_id": [], "review_status": [], "note": []},
            geometry=[],
            crs=layers.area.crs,
        )
    else:
        frame = gpd.GeoDataFrame(rows, crs=layers.area.crs)
    frame.to_parquet(directory / "review_queue.parquet", index=False)
    frame.drop(columns="geometry").to_csv(directory / "review_queue.csv", index=False)
    frame.to_crs(4326).to_file(directory / "review_queue.geojson", driver="GeoJSON")
    return dict(Counter(frame.category))


def prepare_clean_dataset(config_path):
    settings = load_config(config_path)
    document = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    parameters = document["cleaning"]
    source, raw = settings.data_dir / "source", settings.data_dir / "raw"
    qa = settings.data_dir / "qa"
    raw.mkdir(parents=True, exist_ok=True)
    qa.mkdir(parents=True, exist_ok=True)
    area = gpd.read_parquet(source / "area.parquet").to_crs(settings.metric_crs)
    boundary = area.geometry.union_all()
    features = gpd.read_parquet(source / "osm_features.parquet").to_crs(settings.metric_crs)
    gba_path = (Path(config_path).resolve().parent / parameters["gba_buildings"]).resolve()
    gba = gpd.read_parquet(gba_path)
    buildings, building_audit, unmatched = clean_buildings(
        features, boundary, gba, id_iou=parameters["gba_height_match_iou"]
    )
    building_audit["upstream_geometry_audit"] = str(source / "osm_feature_geometry_audit.json")
    building_audit["all_repaired_buildings_in_core"] = int(buildings.geometry_repaired.sum())
    _records(qa / "building_cleaning.json", building_audit)
    unmatched.to_parquet(qa / "unmatched_historic_gba.parquet", index=False)
    _save_review_layers(features, boundary, qa)
    unsimplified = ox.load_graphml(source / "osm_unsimplified.graphml")
    nodes, edges, directed, network_audit = clean_network(
        unsimplified,
        boundary,
        settings.metric_crs,
        endpoint_tolerance=parameters["endpoint_tolerance_m"],
    )
    context_boundary = gpd.read_parquet(source / "context.parquet").geometry.union_all().boundary
    nodes["context_boundary_distance_m"] = nodes.geometry.distance(context_boundary)
    ox.save_graphml(directed, source / "osm_clean_directed_context.graphml")
    layers = Layers(area, buildings, nodes, edges)
    preliminary = audit_layers(
        layers,
        tolerance=parameters["endpoint_tolerance_m"],
        near_miss_m=parameters["near_miss_review_m"],
    )
    _records(qa / "network_cleaning.json", network_audit)
    _records(qa / "preliminary_audit.json", preliminary)
    if not preliminary["hard_checks_passed"]:
        raise ValueError("Source geometry/topology failed audit; see qa/preliminary_audit.json")
    nodes["component_id"] = nodes.id.map(preliminary["component_by_node"])
    node_review_counts = Counter(item["node_id"] for item in preliminary["near_miss_candidates"])
    nodes["near_miss_review_count"] = nodes.id.map(node_review_counts).fillna(0).astype(int)
    edge_reviews = Counter(item["segment_id"] for item in preliminary["near_miss_candidates"])
    for item in preliminary["crossings_without_shared_node"]:
        if not item["grade_separation_tags_present"]:
            edge_reviews.update([item["a"], item["b"]])
    edges["topology_review_count"] = edges.id.map(edge_reviews).fillna(0).astype(int)
    layers.save(raw)
    provenance = {
        "study_area_id": settings.study_area_id,
        "source": "Audited OSM + matched GBA heights",
        "acquired_at_utc": utc_now(),
        "attribution": "© OpenStreetMap contributors; GBA",
        "license": "OSM/GBA-OSM polygons: ODbL-1.0; GBA heights: CC-BY-NC-4.0",
        "feature_acquisition": json.loads((source / "features_acquisition.json").read_text()),
        "network_acquisition": json.loads((source / "network_acquisition.json").read_text()),
        "context_buffer_m": parameters["context_buffer_m"],
        "retain_context_network": True,
        "require_topology_validation": True,
        "source_sha256": {
            str(p): file_sha256(p)
            for p in [
                source / "osm_features.parquet",
                source / "osm_unsimplified.graphml",
                gba_path,
            ]
        },
        "height_matching": {
            "same_id_min_iou": parameters["gba_height_match_iou"],
            "spatial_only_min_iou": 0.90,
            "requires_one_to_one": True,
        },
        "network_semantics": "Physical OSM walk centerlines, not verified legal routing.",
    }
    write_json(raw / "manifest.json", provenance)
    result, summary = analyze(settings, retain_context=True)
    mm_nearest = momepy.get_nearest_street(
        result.buildings,
        result.street_segments,
        max_distance=settings.nearest_street_max_distance_m,
    )
    distance_checks = []
    for index, street_index in mm_nearest.items():
        recorded = result.buildings.loc[index, "nearest_street_distance_m"]
        if pd.isna(street_index):
            distance_checks.append(bool(pd.isna(recorded)))
        else:
            measured = result.buildings.geometry.loc[index].distance(
                result.street_segments.geometry.loc[street_index]
            )
            distance_checks.append(bool(np.isclose(measured, recorded, atol=1e-8, rtol=0)))
    result.buildings["nearest_distance_crosscheck_passed"] = distance_checks
    if not all(distance_checks):
        raise ValueError("momepy nearest-street distance cross-check failed.")
    result.street_nodes["possible_boundary_dead_end"] = result.street_nodes.street_degree.eq(
        1
    ) & result.street_nodes.context_boundary_distance_m.le(30)
    result.save(settings.data_dir / "processed")
    final = audit_layers(
        result,
        tolerance=parameters["endpoint_tolerance_m"],
        near_miss_m=parameters["near_miss_review_m"],
    )
    final["building_review_counts"] = {
        key: len(building_audit[key])
        for key in [
            "geometry_events",
            "exact_geometry_aliases",
            "ambiguous_height_candidates",
            "substantive_overlaps",
        ]
    }
    final["height_coverage"] = {
        "osm_tag": int(result.buildings.osm_height_m.notna().sum()),
        "gba_matched": int(result.buildings.gba_height_m.notna().sum()),
        "usable_height": int(result.buildings.height_m.notna().sum()),
        "missing": int(result.buildings.height_m.isna().sum()),
        "conflicts": int(result.buildings.height_conflict.sum()),
        "analysis_height_without_conflicts": int(
            result.buildings.height_for_analysis_m.notna().sum()
        ),
    }
    final["unmatched_historic_gba_buildings"] = len(unmatched)
    final["possible_boundary_dead_ends"] = int(result.street_nodes.possible_boundary_dead_end.sum())
    final["momepy_nearest_distance_crosscheck_passed"] = all(distance_checks)
    final["repaired_core_buildings_including_osmnx"] = int(result.buildings.geometry_repaired.sum())
    if not final["hard_checks_passed"]:
        write_json(qa / "report.json", final)
        raise ValueError("Processed geometry/topology failed audit.")
    metadata = export_graph(settings)
    _, exported_nodes, relations = validate_graph(settings.data_dir / "graph")
    # Round trip the property graph into the same physical MultiGraph.
    recovered = nx.MultiGraph()
    recovered.add_nodes_from(n["uid"] for n in exported_nodes if n["label"] == "StreetNode")
    for relation in relations:
        if relation["type"] == "CONNECTS_TO":
            recovered.add_edge(
                relation["source"],
                relation["target"],
                key=relation["properties"]["segment_id"],
                length=relation["properties"]["length_m"],
            )
    roundtrip = (
        len(recovered) == len(result.street_nodes)
        and recovered.number_of_edges() == len(result.street_segments)
        and sorted(map(len, nx.connected_components(recovered))) == sorted(final["component_sizes"])
        and np.isclose(
            sum(d["length"] for *_, d in recovered.edges(data=True)),
            result.street_segments.length_m.sum(),
        )
    )
    final["property_graph_roundtrip_passed"] = bool(roundtrip)
    final["graph_counts"] = {"nodes": metadata["nodes"], "relationships": metadata["relationships"]}
    final["source_completeness_verified"] = False
    final["public_access_verified"] = False
    final["review_queue_counts"] = _save_review_queue(result, building_audit, final, qa)
    write_json(qa / "report.json", final)
    if not roundtrip:
        raise ValueError("Property graph roundtrip mismatch.")
    # Portable GIS deliverables; full context network is retained in every graph format.
    for name in ["area", "buildings", "street_nodes", "street_segments"]:
        frame = getattr(result, name)
        frame.to_crs(4326).to_file(
            settings.data_dir / "processed" / f"{name}.geojson", driver="GeoJSON"
        )
    return {
        "study_area_id": settings.study_area_id,
        "buildings": summary["building_count"],
        "context_nodes": len(nodes),
        "context_segments": len(edges),
        "hard_checks_passed": True,
        "graph_roundtrip_passed": bool(roundtrip),
        "qa_report": str(qa / "report.json"),
    }
