"""Geometric oracles and interchange integrity, not assertions copied from the code."""

import json
import math
from dataclasses import replace

import geopandas as gpd
import pytest

from urban_fabric.cli import main
from urban_fabric.config import load_config
from urban_fabric.data import Layers
from urban_fabric.demo import make_demo
from urban_fabric.graph import export_graph, validate_graph
from urban_fabric.metrics import analyze, building_neighbors, parse_height
from urban_fabric.neo4j_store import import_graph


@pytest.fixture
def calculated(tmp_path):
    settings = make_demo(tmp_path / "demo")
    layers, summary = analyze(settings)
    return settings, layers, summary


def test_known_geometry_and_network(calculated):
    _, layers, summary = calculated
    assert summary["area_m2"] == pytest.approx(160_000)
    assert summary["street_length_in_area_m"] == pytest.approx(800)
    assert summary["intersection_count"] == 1
    assert summary["intersection_density_per_km2"] == pytest.approx(6.25)
    # Last footprint extends beyond the AOI; only 200 of its 600 m² is inside.
    assert summary["building_footprint_union_in_area_m2"] == pytest.approx(5400)
    assert summary["building_coverage_ratio"] == pytest.approx(5400 / 160_000)
    first = layers.buildings.set_index("id").loc["demo:building:0"]
    assert first.area_m2 == pytest.approx(600)
    assert first.perimeter_m == pytest.approx(100)
    assert first.isoperimetric_quotient == pytest.approx(4 * math.pi * 600 / 100**2)
    assert first.rectangularity == pytest.approx(1)
    assert first.circular_compactness == pytest.approx(600 / (math.pi * 325))
    assert first.nearest_street_distance_m == pytest.approx(140)
    assert len(layers.buildings) == 6  # Equal-distance ties must not duplicate buildings.


def test_symmetric_neighbors_are_emitted_once(calculated):
    _, layers, _ = calculated
    pairs = building_neighbors(layers.buildings, 20)
    assert len(pairs) == 1
    assert pairs[0]["source_id"] == "demo:building:0"
    assert pairs[0]["target_id"] == "demo:building:1"
    assert pairs[0]["distance_m"] == pytest.approx(10)


def test_overlapping_footprints_do_not_double_count_coverage(calculated):
    settings, _, before = calculated
    raw = Layers.load(settings.data_dir / "raw")
    duplicate = raw.buildings.iloc[[0]].copy()
    duplicate["id"] = "demo:building:duplicate"
    import pandas as pd

    raw.buildings = gpd.GeoDataFrame(
        pd.concat([raw.buildings, duplicate], ignore_index=True), crs=raw.buildings.crs
    )
    raw.save(settings.data_dir / "raw")
    _, after = analyze(settings)
    assert after["building_count"] == before["building_count"] + 1
    assert after["building_coverage_ratio"] == pytest.approx(before["building_coverage_ratio"])


def test_geographic_crs_is_rejected(calculated):
    settings, _, _ = calculated
    with pytest.raises(ValueError, match="米制"):
        analyze(replace(settings, metric_crs="EPSG:4326"))


def test_duplicate_source_identifiers_are_rejected(calculated):
    settings, _, _ = calculated
    raw = Layers.load(settings.data_dir / "raw")
    raw.buildings.loc[1, "id"] = raw.buildings.loc[0, "id"]
    raw.save(settings.data_dir / "raw")
    with pytest.raises(ValueError, match="唯一"):
        analyze(settings)


def test_old_osm_query_cannot_be_relabelled_as_new_area(calculated):
    settings, _, _ = calculated
    path = settings.data_dir / "raw/manifest.json"
    path.write_text(
        json.dumps(
            {
                "study_area_id": settings.study_area_id,
                "source": "OpenStreetMap",
                "query": {"bbox_wsen": [1, 2, 3, 4]},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="重新运行 fetch"):
        analyze(replace(settings, bbox=(118, 31, 119, 32)))


def test_missing_nearest_street_is_not_fabricated(calculated):
    settings, _, _ = calculated
    restricted = replace(settings, nearest_street_max_distance_m=1)
    layers, _ = analyze(restricted)
    export_graph(restricted)
    _, _, relationships = validate_graph(settings.data_dir / "graph")
    assert layers.buildings.nearest_street_id.isna().all()
    assert not any(row["type"] == "NEAR_STREET" for row in relationships)


def test_export_integrity_and_stable_identifiers(calculated):
    settings, _, _ = calculated
    first = export_graph(settings)
    _, nodes, relationships = validate_graph(settings.data_dir / "graph")
    ids = [r["uid"] for r in nodes], [r["uid"] for r in relationships]
    assert first["nodes"] == 16  # 1 area + 6 buildings + 5 nodes + 4 street segments.
    assert first["source_manifest"]["source"] == "SYNTHETIC"
    assert sum(r["type"] == "CONNECTS_TO" for r in relationships) == 4
    assert all(
        not r["properties"]["access_verified"] for r in relationships if r["type"] == "NEAR_STREET"
    )
    export_graph(settings)
    _, nodes2, relationships2 = validate_graph(settings.data_dir / "graph")
    assert ids == ([r["uid"] for r in nodes2], [r["uid"] for r in relationships2])


def test_changed_threshold_requires_recalculation(calculated):
    settings, _, _ = calculated
    with pytest.raises(ValueError, match="重新运行"):
        export_graph(replace(settings, building_neighbor_distance_m=40))


def test_dry_run_never_connects(calculated, monkeypatch):
    settings, _, _ = calculated
    export_graph(settings)
    monkeypatch.setattr(
        "urban_fabric.neo4j_store.GraphDatabase.driver",
        lambda *a, **k: pytest.fail("dry-run attempted network access"),
    )
    assert import_graph(settings.data_dir / "graph", dry_run=True)["dry_run"]


def test_broken_graph_rejected_before_database_mutation(calculated, monkeypatch):
    settings, _, _ = calculated
    export_graph(settings)
    directory = settings.data_dir / "graph"
    rows = [
        json.loads(line) for line in (directory / "relationships.jsonl").read_text().splitlines()
    ]
    rows[0]["target"] = "nonexistent"
    (directory / "relationships.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n"
    )
    monkeypatch.setattr(
        "urban_fabric.neo4j_store.GraphDatabase.driver",
        lambda *a, **k: pytest.fail("invalid graph reached the database"),
    )
    with pytest.raises(ValueError, match="不存在"):
        import_graph(directory)


def test_config_paths_are_relative_to_file(tmp_path):
    path = tmp_path / "configs" / "example.yaml"
    path.parent.mkdir()
    path.write_text(
        "study_area:\n  id: sample\n  bbox: [118, 31, 119, 32]\n"
        "paths:\n  data_dir: ../data/sample\n"
    )
    assert load_config(path).data_dir == tmp_path / "data/sample"
    path.write_text("study_area:\n  id: sample\n  bbox: [119, 31, 118, 32]\n")
    with pytest.raises(ValueError, match="bbox"):
        load_config(path)


def test_height_units_and_missing_values():
    assert parse_height("20 ft") == pytest.approx(6.096)
    assert parse_height("12 m") == 12
    assert parse_height("12;15") is None
    assert parse_height("unknown") is None


def test_cli_demo_and_import_validation(tmp_path, capsys):
    directory = tmp_path / "demo"
    assert main(["demo", "--output", str(directory)]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["building_count"] == 6
    assert main(["import-neo4j", "--graph-dir", str(directory / "graph"), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"]
