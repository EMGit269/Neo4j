from pathlib import Path

import geopandas as gpd
import pytest
import yaml
from shapely.geometry import Point, Polygon, box

from urban_fabric.pois import classify_poi, filter_features


@pytest.fixture
def rules():
    return yaml.safe_load((Path(__file__).parents[1] / "configs/osm-poi-filter.yaml").read_text())


@pytest.mark.parametrize(
    "tags,reason",
    [
        ({"natural": "tree", "name": "古树"}, "tree_or_tree_row"),
        ({"highway": "street_lamp"}, "street_lamp"),
        ({"emergency": "fire_hydrant", "name": "消防栓"}, "fire_facility"),
        ({"amenity": "fire_station"}, "fire_facility"),
        ({"amenity": "bench"}, "small_equipment_or_non_poi"),
        ({"tourism": "information", "information": "board"}, "information_board_or_sign"),
        ({"power": "pole"}, "power_or_telecom_equipment"),
        ({"disused:shop": "bakery"}, "inactive_function_tag"),
        ({"shop": "vacant"}, "inactive_function_tag"),
    ],
)
def test_exclude_user_unwanted_and_equipment(tags, reason, rules):
    result = classify_poi(tags, rules)
    assert result.decision == "excluded"
    assert result.reason.startswith(reason)


def test_function_overrides_background_equipment_tags_but_unnamed_is_kept(rules):
    assert classify_poi({"amenity": "school"}, rules).category == "education"
    result = classify_poi({"amenity": "cafe", "power": "generator"}, rules)
    assert result.decision == "included"
    assert result.category == "food"
    assert classify_poi({"building": "school", "name": "旧校舍"}, rules).decision == "review"
    assert (
        classify_poi({"leisure": "garden", "garden:type": "residential"}, rules).decision
        == "context"
    )


def test_boundary_points_distinct_functions_and_conservative_entity_merging(rules):
    rows = [
        {
            "osm_uid": "osm:way:1",
            "osm_type": "way",
            "osm_id": "1",
            "name": "甲校",
            "amenity": "school",
            "geometry": box(0, 0, 20, 20),
        },
        {
            "osm_uid": "osm:node:2",
            "osm_type": "node",
            "osm_id": "2",
            "name": "甲校",
            "amenity": "school",
            "geometry": Point(10, 10),
        },
        {
            "osm_uid": "osm:node:3",
            "osm_type": "node",
            "osm_id": "3",
            "name": "甲校",
            "amenity": "cafe",
            "geometry": Point(12, 12),
        },
        {
            "osm_uid": "osm:node:4",
            "osm_type": "node",
            "osm_id": "4",
            "name": "甲校",
            "amenity": "school",
            "geometry": Point(25, 25),
        },
        {
            "osm_uid": "osm:way:5",
            "osm_type": "way",
            "osm_id": "5",
            "name": "跨界公园",
            "leisure": "park",
            "geometry": box(20, 20, 60, 60),
        },
        {
            "osm_uid": "osm:node:6",
            "osm_type": "node",
            "osm_id": "6",
            "natural": "tree",
            "geometry": Point(4, 4),
        },
        {
            "osm_uid": "osm:node:7",
            "osm_type": "node",
            "osm_id": "7",
            "amenity": "restaurant",
            "geometry": Point(41, 41),
        },
    ]
    source = gpd.GeoDataFrame(rows, crs=32651)
    area = gpd.GeoDataFrame(geometry=[box(0, 0, 30, 30)], crs=32651)
    audit, members, canonical, points = filter_features(source, area, rules)
    assert len(audit) == 7
    assert len(members) == 5
    assert len(canonical) == 4
    dup = members[members.osm_uid == "osm:node:2"].iloc[0]
    assert dup.duplicate_of == "osm:way:1"
    assert "osm:node:3" in set(points.osm_uid)  # A cafe in a school remains a different function.
    assert "osm:node:4" in set(points.osm_uid)  # Same name outside the campus is not merged.
    assert points.geometry.covered_by(area.geometry.iloc[0]).all()
    assert points[points.osm_uid == "osm:way:5"].crosses_core_boundary.iloc[0]


def test_representative_point_inside_concave_area_and_preserve_transport_roles(rules):
    concave = Polygon([(0, 0), (10, 0), (10, 2), (2, 2), (2, 10), (0, 10)])
    source = gpd.GeoDataFrame(
        [
            {
                "osm_uid": "osm:way:1",
                "osm_type": "way",
                "osm_id": "1",
                "amenity": "school",
                "geometry": concave,
            },
            {
                "osm_uid": "osm:node:2",
                "osm_type": "node",
                "osm_id": "2",
                "name": "同名站",
                "railway": "station",
                "geometry": Point(15, 15),
            },
            {
                "osm_uid": "osm:node:3",
                "osm_type": "node",
                "osm_id": "3",
                "name": "同名站",
                "railway": "subway_entrance",
                "geometry": Point(15, 15),
            },
        ],
        crs=32651,
    )
    area = gpd.GeoDataFrame(geometry=[box(-1, -1, 30, 30)], crs=32651)
    _, _, _, points = filter_features(source, area, rules)
    assert concave.covers(points[points.osm_uid == "osm:way:1"].geometry.iloc[0])
    assert set(points.poi_role) == {"place", "station", "station_entrance"}
