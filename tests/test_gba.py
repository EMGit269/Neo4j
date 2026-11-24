"""GBA source-ID joins, publisher CRS correction, and complete district boundaries."""

import json

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, Polygon, box, mapping

from urban_fabric.gba import district_boundary, process_district
from urban_fabric.graph import validate_graph


def boundary_document():
    corners = [(118.7, 32.0), (118.71, 32.0), (118.71, 32.01), (118.7, 32.01)]
    members = []
    for a, b in zip(corners, corners[1:] + corners[:1], strict=True):
        members.append(
            {
                "type": "way",
                "role": "outer",
                "geometry": [{"lon": x, "lat": y} for x, y in (a, b)],
            }
        )
    return {
        "elements": [
            {
                "id": 123,
                "type": "relation",
                "tags": {"boundary": "administrative", "division_code": "320106"},
                "members": members,
            }
        ]
    }


def test_boundary_rejects_wrong_district_or_open_ring():
    document = boundary_document()
    _, geometry = district_boundary(document, "320106")
    assert geometry.equals(box(118.7, 32.0, 118.71, 32.01))
    with pytest.raises(ValueError, match="division_code"):
        district_boundary(document, "320102")
    document["elements"][0]["members"].pop()
    with pytest.raises(ValueError, match="未闭合"):
        district_boundary(document, "320106")


@pytest.mark.parametrize("parts", [["odbl", "polygon"], ["polygon"]])
def test_gba_district_joins_height_by_source_key_and_uses_publisher_crs(
    tmp_path, parts, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    boundary = source / "boundary.json"
    boundary.write_text(json.dumps(boundary_document()))
    footprints = gpd.GeoSeries(
        [
            box(118.701, 32.001, 118.702, 32.002),
            box(118.709, 32.003, 118.711, 32.004),
            box(118.703, 32.005, 118.704, 32.006),
            box(118.72, 32.02, 118.721, 32.021),
        ],
        crs=4326,
    ).to_crs(3857)
    features = [
        {
            "type": "Feature",
            "properties": {"source": "clsm", "id": str(index), "region": "CHN"},
            "geometry": mapping(polygon),
        }
        for index, polygon in enumerate(footprints)
    ]
    files = []
    for kind in ("polygon", "odbl"):
        path = source / (kind + ".geojson")
        # Deliberately wrong CRS declaration; coordinates follow the publisher's EPSG:3857.
        path.write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
                    "features": features if kind == "polygon" else [],
                }
            )
        )
        files.append({"kind": kind, "local_path": str(path)})
    height_file = source / "height.json"
    height_file.write_text(
        json.dumps(
            {
                "clsm0CHN": {"height": 12.5, "var": 2.0},
                "clsm1CHN": {"height": 0, "var": 1.0},
                "clsm3CHN": {"height": 99, "var": 2.0},
            }
        )
    )
    files.append({"kind": "height", "local_path": str(height_file)})
    summary = process_district(
        {
            "id": "gba_test",
            "name": "Synthetic district",
            "division_code": "320106",
            "boundary_file": boundary,
            "data_dir": tmp_path,
            "tile": "synthetic",
            "metric_crs": "EPSG:32650",
            "polygon_parts": parts,
        },
        files,
        progress=lambda _: None,
    )
    assert summary["building_count"] == 3
    assert summary["valid_height_count"] == 1
    assert summary["missing_height_count"] == 2
    assert summary["height_record_unmatched_count"] == 1
    assert summary["boundary_building_count"] == 1
    assert summary["partial_building_sample"] == (len(parts) == 1)
    buildings = gpd.read_parquet(tmp_path / "processed/buildings.parquet").set_index("id")
    assert buildings.loc["gba:clsm:CHN:0", "height_m"] == 12.5
    assert buildings.loc["gba:clsm:CHN:0", "gba_var_raw"] == 2.0
    assert buildings.loc["gba:clsm:CHN:1", "area_in_district_m2"] == pytest.approx(
        buildings.loc["gba:clsm:CHN:1", "area_m2"] / 2, rel=0.001
    )
    metadata, nodes, relationships = validate_graph(tmp_path / "graph")
    assert metadata["nodes"] == 4
    assert len(relationships) == 3
    assert all(row["type"] == "PART_OF_DATASET" for row in relationships)
    assert all(row["label"] in {"StudyArea", "Building"} for row in nodes)
    if parts == ["polygon"]:
        from urban_fabric.gba_streets import add_osm_streets

        street_nodes = gpd.GeoDataFrame(
            {"id": ["osm:node:1", "osm:node:2"]},
            geometry=[Point(118.7, 32.001), Point(118.71, 32.001)],
            crs=4326,
        )
        street_segments = gpd.GeoDataFrame(
            {
                "id": ["osm:segment:1:2:0"],
                "u": ["osm:node:1"],
                "v": ["osm:node:2"],
                "highway": ["residential"],
                "name": ["Synthetic street"],
            },
            geometry=[LineString([(118.7, 32.001), (118.71, 32.001)])],
            crs=4326,
        )
        monkeypatch.setattr(
            "urban_fabric.gba_streets.fetch_street_network",
            lambda *args: (street_nodes, street_segments),
        )
        updated = add_osm_streets(tmp_path)
        assert updated["quality"]["missing_height_count"] == 2
        assert updated["street_segments"] == 1
        _, nodes, relations = validate_graph(tmp_path / "graph")
        assert any(row["type"] == "NEAR_STREET" for row in relations)
        for row in nodes:
            source = row["properties"]["source"]
            if row["label"] == "Building":
                assert source == "GlobalBuildingAtlas"
                if row["properties"]["source_id"] == "gba:clsm:CHN:0":
                    assert row["properties"]["height_m"] == 12.5
            else:
                assert source == "OpenStreetMap"


def test_boundary_preserves_hole():
    document = boundary_document()
    ring = list(box(118.703, 32.003, 118.705, 32.005).exterior.coords)
    document["elements"][0]["members"].append(
        {
            "type": "way",
            "role": "inner",
            "geometry": [{"lon": x, "lat": y} for x, y in ring],
        }
    )
    _, geometry = district_boundary(document, "320106")
    assert len(geometry.interiors) == 1
    assert not geometry.covers(Polygon(ring).centroid)
