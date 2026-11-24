import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString, MultiPolygon, box

from urban_fabric.georeferencing import (
    GeographicModelTransform,
    calibrate_geographic_model,
    verify_layer_roundtrip,
)
from urban_fabric.rhino import LOCAL_METRES


def test_transform_roundtrip_and_metric_scale():
    transform = GeographicModelTransform(12_035_810.241736354, 3_128_482.8565913253)
    local = gpd.GeoDataFrame(
        {"id": ["area"]}, geometry=[box(23000, 0, 29000, 5000)], crs=LOCAL_METRES
    )
    geographic = transform.to_wgs84(local)
    assert geographic.crs.to_epsg() == 4326
    assert geographic.total_bounds == pytest.approx(
        [120.58810241736354, 31.28482856591325, 120.64810241736354, 31.33482856591325]
    )
    recovered = transform.to_local_geometry(geographic.geometry.to_numpy())[0]
    assert recovered.hausdorff_distance(local.geometry.iloc[0]) < 1e-7
    # The model declaration cannot be used as ground metres (old value was 30 km²).
    assert 31.5e6 < geographic.to_crs(32651).area.iloc[0] < 31.8e6
    with pytest.raises(ValueError, match="double conversion"):
        transform.to_wgs84(geographic)


def test_calibration_spatial_holdout_rejects_parallel_false_matches():
    rng = np.random.default_rng(151)
    starts = rng.uniform([0, 0], [40000, 20000], size=(300, 2))
    delta = rng.uniform([60, -300], [500, 300], size=(300, 2))
    lines = [LineString([p, p + d]) for p, d in zip(starts, delta)]
    local = gpd.GeoDataFrame({"id": [str(i) for i in range(300)]}, geometry=lines, crs=LOCAL_METRES)
    expected = GeographicModelTransform(12000000, 3100000)
    reference = expected.to_wgs84(local)
    # Equal vectors at other positions must not become controls.
    wrong = reference.iloc[:10].copy()
    wrong.geometry = wrong.translate(xoff=0.5, yoff=0.2)
    reference = pd.concat([reference, wrong], ignore_index=True)
    actual, controls, counts = calibrate_geographic_model(local, reference)
    assert actual.offset_x == pytest.approx(expected.offset_x, abs=1e-8)
    assert actual.offset_y == pytest.approx(expected.offset_y, abs=1e-8)
    assert counts["accepted_segments"] == 300
    assert set(controls.partition) == {"fit", "validation"}
    assert controls[["residual_m", "end_residual_m"]].to_numpy().max() < 1e-6


def test_calibration_rejects_incorrect_scale_hypothesis():
    local = gpd.GeoDataFrame(geometry=[LineString([(0, 0), (100, 17)])], crs=LOCAL_METRES)
    reference = GeographicModelTransform(12000000, 3100000).to_wgs84(local)
    with pytest.raises(ValueError):
        calibrate_geographic_model(local, reference, units_per_degree=111320)


def test_geopackage_mixed_polygon_types_and_filtered_indices(tmp_path):
    expected = gpd.GeoDataFrame(
        {"id": ["a", "b"]},
        geometry=[box(0, 0, 10, 10), MultiPolygon([box(20, 0, 30, 10)])],
        crs=32651,
        index=[17, 905],
    )
    path = tmp_path / "mixed.gpkg"
    expected.to_file(path, driver="GPKG", layer="buildings")
    recovered = gpd.read_file(path, layer="buildings")
    verify_layer_roundtrip(expected, recovered)
    # Moving one object while preserving the total bounds still fails the geometry check.
    changed = recovered.copy()
    changed.loc[0, "geometry"] = box(0, 0, 9, 10)
    with pytest.raises(ValueError, match="geometry mismatch"):
        verify_layer_roundtrip(expected, changed)
