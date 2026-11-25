import json
from dataclasses import replace

import geopandas as gpd
import numpy as np
import pytest
import shapely
from shapely.affinity import rotate, translate
from shapely.geometry import LineString, Polygon, box
from shapely.ops import split

from urban_fabric.rhino import LOCAL_METRES
from urban_fabric.seam_repair import (
    SeamParameters,
    find_seam_candidates,
    infer_grid_lines,
    merge_geometry,
    repair_seams,
)


def fixture_source():
    rows = []
    for x in [0, 100, 200]:
        for y in [0, 100]:
            outline = translate(rotate(box(-10, -12, 10, 12), 15), xoff=x, yoff=y)
            cut = LineString([(x, y - 40), (x, y + 40)])
            for i, geometry in enumerate(split(outline, cut).geoms):
                rows.append(
                    {
                        "id": f"part:{len(rows)}",
                        "component_id": len(rows),
                        "height_extent_m": 3.0 if i == 0 else 6.0,
                        "footprint_requires_review": False,
                        "geometry": geometry,
                    }
                )
    return gpd.GeoDataFrame(rows, crs=LOCAL_METRES)


@pytest.fixture
def parameters():
    return replace(SeamParameters(), grid_minimum_pairs=2, grid_minimum_spatial_span=50)


def test_repeated_grid_repair_preserves_coverage_ids_and_uses_maximum_height(parameters):
    source = fixture_source()
    before = source.geometry.union_all()
    result, members, _, report = repair_seams(source, parameters)
    assert len(result) == 6
    assert report["merged_groups"] == 6
    assert set(members.source_id) == set(source.id)
    assert members.source_id.is_unique
    assert result.height_extent_m.eq(6).all()
    assert result.height_conflict.all()
    assert result.model_height_min_m.eq(3).all()
    assert result.model_height_max_m.eq(6).all()
    assert all(json.loads(v) == [3.0, 6.0] for v in result.model_height_values_json)
    assert result.height_aggregation_method.eq("max_source_component_height").all()
    assert before.symmetric_difference(result.geometry.union_all()).area < 1e-8
    again, again_members, _, _ = repair_seams(source, parameters)
    assert result.id.tolist() == again.id.tolist()
    assert members.equals(again_members)


def test_corner_contacts_off_grid_and_different_outlines_remain_separate(parameters):
    source = fixture_source()
    extra = [
        box(45, 30, 50, 40),
        box(50, 30, 58, 40),  # A shared wall off the supported grid.
        box(290, 0, 300, 10),
        box(300, 10, 310, 20),  # Corner contact only.
        box(-10, 250, 0, 260),
        box(0, 255, 20, 268),  # On grid, exterior does not continue.
    ]
    more = gpd.GeoDataFrame(
        [
            {
                "id": f"extra:{i}",
                "component_id": 100 + i,
                "height_extent_m": 6.0,
                "footprint_requires_review": False,
                "geometry": g,
            }
            for i, g in enumerate(extra)
        ],
        crs=LOCAL_METRES,
    )
    import pandas as pd

    source = pd.concat([source, more], ignore_index=True)
    result, members, _, _ = repair_seams(source, parameters)
    assert len(result) == 12
    retained = members[members.source_id.str.startswith("extra:")]
    assert not retained.seam_merged.any()


def test_no_repeated_grid_does_not_merge_even_touching_rectangles(parameters):
    source = fixture_source().iloc[:2].reset_index(drop=True)
    candidates = find_seam_candidates(source, parameters)
    assert infer_grid_lines(candidates, parameters) == []
    result, _, _, _ = repair_seams(source, parameters)
    assert len(result) == 2


def test_union_preserves_courtyard_and_does_not_close_real_gaps(parameters):
    courtyard = Polygon(
        box(-20, -20, 20, 20).exterior.coords, holes=[box(-5, -5, 5, 5).exterior.coords]
    )
    pieces = list(split(courtyard, LineString([(0, -40), (0, 40)])).geoms)
    repaired, method = merge_geometry(pieces, parameters)
    assert method == "exact_union"
    assert len(repaired.interiors) == 1
    assert repaired.equals(courtyard)
    separated = [box(0, 0, 10, 10), box(11, 0, 21, 10)]
    repaired, reason = merge_geometry(separated, parameters)
    assert repaired is None
    assert reason == "remaining_gap_or_multipart"


def test_local_precision_merge_has_bounded_area_change(parameters):
    originals = [box(0, 0, 10, 10), box(10.02, 0, 20, 10)]
    repaired, method = merge_geometry(originals, parameters)
    assert method == "mutual_seam_snap_union"
    assert repaired.geom_type == "Polygon"
    assert repaired.symmetric_difference(shapely.union_all(originals)).area < 0.3
    assert repaired.hausdorff_distance(shapely.union_all(originals)) < 0.03
    with pytest.raises(ValueError):
        replace(parameters, neighbor_tolerance=np.nan)


def test_precision_merge_does_not_mistake_erased_internal_vertices_for_movement(parameters):
    originals = shapely.segmentize([box(0, 0, 10, 10), box(10.02, 0, 20, 10)], 1)
    # Source vertices halfway along the seam disappear inside the merged polygon.
    # Their distance to the new exterior is not a change in occupied footprint.
    repaired, method = merge_geometry(originals, parameters)
    assert method == "mutual_seam_snap_union"
    assert repaired is not None and repaired.is_valid
    assert repaired.symmetric_difference(shapely.union_all(originals)).area < 0.3


def test_internal_multipart_grid_seam_is_dissolved_in_requested_area(parameters):
    from shapely.geometry import MultiPolygon, box

    from urban_fabric.seam_repair import _merge_internal_parts

    frame = gpd.GeoDataFrame(
        {
            "work_id": [0],
            "id": ["source:1"],
            "geometry": [
                MultiPolygon([box(0, 0, 10, 10), box(10.02, 0, 20, 10), box(30, 0, 35, 5)])
            ],
        },
        geometry="geometry",
        crs=LOCAL_METRES,
    )
    area = box(-1, -1, 25, 11)
    repaired, events = _merge_internal_parts(
        frame,
        parameters,
        [
            {
                "axis": "x",
                "minimum_coordinate": 9.9,
                "maximum_coordinate": 10.1,
            }
        ],
        area,
        merge_all_grid_candidates=False,
    )
    assert len(events) == 1
    assert repaired.geometry.iloc[0].geom_type == "MultiPolygon"
    assert len(shapely.get_parts(repaired.geometry.iloc[0])) == 2
    assert repaired.geometry.iloc[0].covers(box(0, 0, 20, 10).buffer(-0.01))


def test_user_requested_grid_seams_merge_even_when_outer_edges_do_not_continue(parameters):
    import pandas as pd

    source = fixture_source()
    extras = gpd.GeoDataFrame(
        [
            {
                "id": "extra:left",
                "component_id": 9001,
                "height_extent_m": 3.0,
                "footprint_requires_review": False,
                "geometry": box(-10, 250, 0, 260),
            },
            {
                "id": "extra:right",
                "component_id": 9002,
                "height_extent_m": 9.0,
                "footprint_requires_review": False,
                "geometry": box(0, 255, 20, 268),
            },
        ],
        crs=LOCAL_METRES,
    )
    source = gpd.GeoDataFrame(
        pd.concat([source, extras], ignore_index=True),
        geometry="geometry",
        crs=LOCAL_METRES,
    )
    result, membership, _, _ = repair_seams(source, parameters, merge_all_grid_candidates=True)
    ids = membership[membership.source_id.str.startswith("extra:")].building_id.unique()
    assert len(ids) == 1
    merged = result[result.id == ids[0]].iloc[0]
    assert merged.height_extent_m == 9
    assert merged.height_conflict
    assert not result.remaining_seam_review.any()
