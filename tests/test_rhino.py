"""Known solid geometry verifies mesh splitting, lower-face union and vertical extent."""

import numpy as np
import pytest

from urban_fabric.rhino import audit_footprint_overlaps, component_footprints, face_components


def cube(x=0, y=0, z=0, height=12):
    vertices = np.array(
        [
            [x, y, z],
            [x + 10, y, z],
            [x + 10, y + 20, z],
            [x, y + 20, z],
            [x, y, z + height],
            [x + 10, y, z + height],
            [x + 10, y + 20, z + height],
            [x, y + 20, z + height],
        ],
        dtype=float,
    )
    faces = np.array(
        [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4], [1, 2, 6, 5], [2, 3, 7, 6], [3, 0, 4, 7]]
    )
    return vertices, faces


def test_unwelded_faces_form_one_cube_with_known_area_and_height():
    vertices, faces = cube(z=100, height=12)
    separated = vertices[faces].reshape(-1, 3)
    independent = np.arange(len(separated)).reshape(-1, 4)
    labels, report = face_components(separated, independent)
    rows, review = component_footprints(separated, independent, labels, "synthetic")
    assert report["components"] == 1
    assert report["edge_incidence_one"] == 0
    assert not review
    assert rows[0]["geometry"].area == pytest.approx(200)
    assert rows[0]["height_extent_m"] == 12
    assert rows[0]["base_z_m"] == 100
    assert rows[0]["top_z_m"] == 112


def test_xy_overlap_and_corner_contact_do_not_merge_solids():
    a, af = cube()
    b, bf = cube(z=30)
    c, cf = cube(x=10, y=20, z=12)
    vertices = np.vstack([a, b, c])
    faces = np.vstack([af, bf + 8, cf + 16])
    labels, report = face_components(vertices, faces)
    rows, review = component_footprints(vertices, faces, labels, "synthetic")
    assert report["components"] == 3
    assert len(rows) == 3 and not review
    assert all(row["height_extent_m"] == 12 for row in rows)


def test_flat_or_without_ground_faces_are_reviewed():
    vertices = np.array([[0, 0, 0], [10, 0, 0], [10, 10, 0], [0, 10, 0]])
    faces = np.array([[0, 1, 2, 3]])
    labels, _ = face_components(vertices, faces)
    rows, review = component_footprints(vertices, faces, labels, "flat")
    assert not rows and review[0]["reason"] == "no_positive_vertical_extent"


def test_full_face_touching_solids_are_explicitly_flagged_for_review():
    a, af = cube()
    b, bf = cube(x=10)
    labels, report = face_components(np.vstack([a, b]), np.vstack([af, bf + 8]))
    assert report["components"] == 1
    assert report["components_with_nonmanifold_edges"] == 1
    assert report["edge_incidence_over_two"] > 0


def test_invalid_vertex_reference_is_rejected():
    vertices, faces = cube()
    faces[0, 0] = 900
    with pytest.raises(ValueError, match="Invalid mesh"):
        face_components(vertices, faces)


def test_base_outline_preserves_concavity_instead_of_bounding_rectangle():
    vertices, faces = cube()
    # Replace the cuboid's base by a known L-shaped planar patch, same component.
    bottom = np.array(
        [
            [0, 0, 0],
            [10, 0, 0],
            [10, 5, 0],
            [0, 5, 0],
            [0, 5, 0],
            [5, 5, 0],
            [5, 20, 0],
            [0, 20, 0],
            [0, 0, 12],
            [10, 0, 12],
            [10, 20, 12],
            [0, 20, 12],
        ]
    )
    faces = np.array([[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11]])
    labels = np.zeros(3, dtype=int)
    rows, review = component_footprints(bottom, faces, labels, "synthetic-L")
    assert not review
    assert rows[0]["geometry"].area == pytest.approx(125)
    assert rows[0]["geometry"].envelope.area == pytest.approx(200)


def test_overlaps_are_marked_without_discarding_candidates():
    import geopandas as gpd
    from shapely.geometry import box

    buildings = gpd.GeoDataFrame(
        {"id": ["a", "b", "c"]}, geometry=[box(0, 0, 10, 10), box(5, 0, 15, 10), box(15, 0, 25, 10)]
    )
    buildings["area_m2"] = buildings.area
    overlaps = audit_footprint_overlaps(buildings)
    assert len(buildings) == 3
    assert len(overlaps) == 1
    assert overlaps[0]["overlap_m2"] == pytest.approx(50)
    assert buildings.footprint_overlap_review.tolist() == [True, True, False]


def test_real_rhino_file_roundtrip_to_local_analysis_layers(tmp_path):
    import geopandas as gpd

    rhino = pytest.importorskip("rhino3dm")
    from urban_fabric.rhino import export_local_geopackage, inspect_model

    model = rhino.File3dm()
    model.Settings.ModelUnitSystem = rhino.UnitSystem.Meters
    layer = rhino.Layer()
    layer.Name = "建筑"
    layer_index = model.Layers.Add(layer)
    vertices, faces = cube(z=100, height=12)
    mesh = rhino.Mesh()
    for point in vertices:
        mesh.Vertices.Add(*point)
    for face in faces:
        mesh.Faces.AddFace(*map(int, face))
    attributes = rhino.ObjectAttributes()
    attributes.LayerIndex = layer_index
    model.Objects.AddMesh(mesh, attributes)
    road_layer = rhino.Layer()
    road_layer.Name = "raod"
    road_index = model.Layers.Add(road_layer)
    road_attributes = rhino.ObjectAttributes()
    road_attributes.LayerIndex = road_index
    line = rhino.PolylineCurve([rhino.Point3d(-10, 0, 0), rhino.Point3d(30, 0, 0)])
    model.Objects.AddCurve(line, road_attributes)
    source = tmp_path / "synthetic.3dm"
    assert model.Write(str(source), 7)
    out = tmp_path / "extracted"
    report = inspect_model(source, out, progress=lambda _: None)
    assert report["extracted"]["building_candidates"] == 1
    assert not report["georeferencing_verified"]
    buildings = gpd.read_parquet(out / "building_candidates.parquet")
    assert buildings.crs.is_engineering
    assert buildings.area_m2.iloc[0] == pytest.approx(200)
    assert buildings.height_extent_m.iloc[0] == 12
    assert not buildings.height_is_survey_verified.iloc[0]
    assert export_local_geopackage(out)["roundtrip_passed"]
