"""Regression cases where generic snapping, ID-only joins or graph flattening are wrong."""

import geopandas as gpd
import networkx as nx
import pytest
from shapely.geometry import LineString, Point, Polygon, box

from urban_fabric.cleaning import audit_layers, clean_buildings, clean_network, orient_line
from urban_fabric.data import Layers
from urban_fabric.demo import make_demo
from urban_fabric.metrics import analyze


def test_repair_keeps_small_footprints_and_missing_heights():
    invalid = Polygon([(0, 0), (2, 2), (0, 2), (2, 0), (0, 0)])
    features = gpd.GeoDataFrame(
        {"id": [1, 2], "element": ["way", "way"], "building": ["yes", "yes"]},
        geometry=[invalid, box(10, 0, 11, 1)],
        crs=32650,
    )
    gba = gpd.GeoDataFrame({"id": []}, geometry=[], crs=32650)
    cleaned, log, _ = clean_buildings(features, box(-1, -1, 20, 20), gba)
    assert len(cleaned) == 2
    assert cleaned.is_valid.all()
    assert cleaned.geometry.area.sum() == pytest.approx(3)
    assert cleaned.height_m.isna().all()
    assert len(log["geometry_events"]) == 1


def test_source_id_does_not_override_changed_geometry():
    features = gpd.GeoDataFrame(
        {"id": [1, 2], "element": ["way", "way"], "building": ["yes", "yes"]},
        geometry=[box(0, 0, 10, 10), box(20, 0, 30, 10)],
        crs=32650,
    )
    gba = gpd.GeoDataFrame(
        {
            "id": ["historic:1", "historic:2"],
            "source_polygon_id": ["1", "2"],
            "polygon_source": ["osm", "osm"],
            "height_m": [99, 12],
        },
        geometry=[box(2, 2, 8, 8), box(20, 0, 30, 10)],
        crs=32650,
    )
    cleaned, _, unmatched = clean_buildings(features, box(-1, -1, 40, 20), gba)
    assert cleaned.loc[0, "gba_match_id"] is None
    assert cleaned.loc[1, "height_m"] == 12
    assert len(unmatched) == 1


def test_ambiguous_height_join_is_not_silently_selected():
    features = gpd.GeoDataFrame(
        {"id": [1], "element": ["way"], "building": ["yes"]},
        geometry=[box(0, 0, 10, 10)],
        crs=32650,
    )
    gba = gpd.GeoDataFrame(
        {
            "id": ["a", "b"],
            "source_polygon_id": ["1", "2"],
            "polygon_source": ["osm", "osm"],
            "height_m": [10, 20],
        },
        geometry=[box(0, 0, 10, 10), box(0, 0, 10, 10)],
        crs=32650,
    )
    cleaned, log, _ = clean_buildings(features, box(-1, -1, 20, 20), gba)
    assert cleaned.height_m.isna().all()
    assert len(log["ambiguous_height_candidates"]) == 2


def _graph(points, edges):
    graph = nx.MultiDiGraph(crs="EPSG:4326", simplified=False)
    for key, (x, y) in points.items():
        graph.add_node(key, x=x, y=y)
    for identifier, u, v, line, tags in edges:
        data = {"osmid": identifier, "length": 20, "highway": "footway", **tags}
        graph.add_edge(u, v, geometry=line, **data)
        graph.add_edge(v, u, geometry=line, **data)
    return graph


def test_grade_crossings_and_real_parallel_roads_are_preserved():
    points = {1: (118.77, 32.06), 2: (118.772, 32.06), 3: (118.771, 32.059), 4: (118.771, 32.061)}
    graph = _graph(
        points,
        [
            (10, 1, 2, LineString([points[1], points[2]]), {}),
            (11, 1, 2, LineString([points[1], (118.771, 32.0602), points[2]]), {}),
            (12, 3, 4, LineString([points[3], points[4]]), {"bridge": "yes", "layer": 1}),
        ],
    )
    core = box(0, 0, 1_000_000, 10_000_000)
    nodes, edges, _, _ = clean_network(graph, core, "EPSG:32650")
    layers = Layers(
        gpd.GeoDataFrame({"id": ["core"]}, geometry=[core], crs=32650),
        gpd.GeoDataFrame({"id": []}, geometry=[], crs=32650),
        nodes,
        edges,
    )
    audit = audit_layers(layers)
    assert audit["hard_checks_passed"]
    assert len(edges) == 3
    assert audit["connected_components"] == 2
    assert audit["parallel_node_pairs"] == 1
    assert len(audit["crossings_without_shared_node"]) == 2


def test_geometry_orientation_is_fixed_but_mismatch_is_rejected():
    line = LineString([(10, 0), (0, 0)])
    oriented, changed = orient_line(line, Point(0, 0), Point(10, 0))
    assert changed
    assert oriented.coords[0] == (0, 0)
    with pytest.raises(ValueError, match="endpoints"):
        orient_line(line, Point(30, 0), Point(40, 0))


def test_context_network_survives_core_reporting_filter(tmp_path):
    settings = make_demo(tmp_path)
    raw = Layers.load(tmp_path / "raw")
    extra_nodes = gpd.GeoDataFrame(
        {"id": ["outside:a", "outside:b"]},
        geometry=[Point(500, 0), Point(600, 0)],
        crs=raw.area.crs,
    )
    extra = gpd.GeoDataFrame(
        {"id": ["outside:edge"], "u": ["outside:a"], "v": ["outside:b"]},
        geometry=[LineString([(500, 0), (600, 0)])],
        crs=raw.area.crs,
    )
    # Demo coordinates live near 500000E, not at the origin; outside geometry remains valid context.
    import pandas as pd

    raw.street_nodes = gpd.GeoDataFrame(
        pd.concat([raw.street_nodes, extra_nodes]), crs=raw.area.crs
    )
    raw.street_segments = gpd.GeoDataFrame(
        pd.concat([raw.street_segments, extra]), crs=raw.area.crs
    )
    raw.save(tmp_path / "raw")
    cleaned, summary = analyze(settings, retain_context=True)
    assert "outside:edge" in set(cleaned.street_segments.id)
    assert summary["street_length_in_area_m"] == pytest.approx(800)
    assert summary["retains_context_network"]


def test_semantic_barrier_node_survives_simplification():
    points = {1: (118.77, 32.06), 2: (118.771, 32.06), 3: (118.772, 32.06)}
    graph = _graph(
        points,
        [
            (10, 1, 2, LineString([points[1], points[2]]), {}),
            (10, 2, 3, LineString([points[2], points[3]]), {}),
        ],
    )
    graph.nodes[2]["barrier"] = "gate"
    nodes, edges, _, _ = clean_network(graph, box(0, 0, 1e6, 1e7), "EPSG:32650")
    assert "osm:node:2" in set(nodes.id)
    assert len(edges) == 2


def test_import_boundary_rejects_corrupt_serialized_topology(tmp_path):
    import json

    from urban_fabric.graph import export_graph, validate_graph

    settings = make_demo(tmp_path)
    manifest = tmp_path / "raw/manifest.json"
    source = json.loads(manifest.read_text())
    source["require_topology_validation"] = True
    manifest.write_text(json.dumps(source))
    analyze(settings)
    export_graph(settings)
    metadata, _, relations = validate_graph(tmp_path / "graph")
    assert metadata["require_topology_validation"]
    next(row for row in relations if row["type"] == "CONNECTS_TO")["properties"]["length_m"] = 1
    (tmp_path / "graph/relationships.jsonl").write_text(
        "\n".join(json.dumps(row) for row in relations) + "\n"
    )
    with pytest.raises(ValueError, match="长度不一致"):
        validate_graph(tmp_path / "graph")


def test_complete_clean_pipeline_with_known_metric_geometry(tmp_path):
    import json

    import osmnx as ox
    import yaml

    from urban_fabric.cleaning import prepare_clean_dataset

    root = tmp_path / "clean"
    source = root / "source"
    source.mkdir(parents=True)
    boundary = box(666000, 3549000, 666400, 3549400)
    area = gpd.GeoDataFrame({"id": ["synthetic_clean"]}, geometry=[boundary], crs=32650)
    area.to_parquet(source / "area.parquet")
    gpd.GeoDataFrame(geometry=area.buffer(500), crs=area.crs).to_parquet(source / "context.parquet")
    area.to_file(tmp_path / "area.geojson", driver="GeoJSON")
    features = gpd.GeoDataFrame(
        {
            "id": ["1", "2"],
            "element": ["way", "way"],
            "building": ["yes", "yes"],
            "height": [None, "12 m"],
        },
        geometry=[box(666030, 3549140, 666060, 3549160), box(666250, 3549270, 666260, 3549290)],
        crs=32650,
    )
    features.to_parquet(source / "osm_features.parquet")
    gba = features.iloc[[0]].copy()
    gba["id"], gba["polygon_source"], gba["source_polygon_id"] = "historic:1", "osm", "1"
    gba["height_m"] = 15.0
    gba.to_parquet(tmp_path / "gba.parquet")
    coords = gpd.GeoSeries(
        [Point(666000, 3549200), Point(666200, 3549200), Point(666650, 3549200)], crs=32650
    ).to_crs(4326)
    points = {i: (p.x, p.y) for i, p in enumerate(coords, 1)}
    graph = _graph(
        points,
        [
            (10, 1, 2, LineString([points[1], points[2]]), {}),
            (10, 2, 3, LineString([points[2], points[3]]), {}),
        ],
    )
    ox.save_graphml(graph, source / "osm_unsimplified.graphml")
    for part in ["features", "network"]:
        (source / f"{part}_acquisition.json").write_text(json.dumps({"source": "SYNTHETIC"}))
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "study_area": {"id": "synthetic_clean", "polygon": "area.geojson"},
                "paths": {"data_dir": "clean"},
                "metrics": {"crs": "EPSG:32650"},
                "cleaning": {
                    "context_buffer_m": 500,
                    "endpoint_tolerance_m": 0.02,
                    "gba_height_match_iou": 0.70,
                    "near_miss_review_m": 2,
                    "gba_buildings": "gba.parquet",
                },
            }
        )
    )
    prepared = prepare_clean_dataset(config)
    result = Layers.load(root / "processed")
    assert prepared["hard_checks_passed"] and prepared["graph_roundtrip_passed"]
    assert len(result.buildings) == 2
    assert sorted(result.buildings.height_m) == [12, 15]
    assert result.street_segments.length_m.sum() == pytest.approx(650, abs=0.02)
    assert result.street_segments.length_in_area_m.sum() == pytest.approx(400, abs=0.02)
    assert sorted(result.buildings.nearest_street_distance_m) == pytest.approx([40, 70], abs=0.02)
