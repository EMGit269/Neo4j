"""Recover scaled geographic model coordinates from matching reference line segments.

This is an explicitly calibrated model transform, not a CRS inferred from Rhino units.
Z is not transformed: exports are 2D and model heights remain separate attributes.
"""

from dataclasses import dataclass

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely


def verify_layer_roundtrip(expected, recovered):
    """Check positional rows without pandas index alignment or container-type assumptions."""
    if len(recovered) != len(expected) or not recovered.crs.equals(expected.crs):
        raise ValueError("GeoPackage count/CRS mismatch.")
    if not np.allclose(recovered.total_bounds, expected.total_bounds, atol=1e-7, rtol=0):
        raise ValueError("GeoPackage bounds mismatch.")
    if "id" in expected and recovered.id.tolist() != expected.id.tolist():
        raise ValueError("GeoPackage ID mismatch.")
    left = recovered.geometry.to_numpy()
    right = expected.geometry.to_numpy()
    # Identical coordinates also work for unchanged invalid reference shapes.
    # Topological equality allows GDAL's Polygon -> MultiPolygon promotion.
    if not (shapely.equals_exact(left, right, 0) | shapely.equals(left, right)).all():
        raise ValueError("GeoPackage geometry mismatch.")


@dataclass(frozen=True)
class GeographicModelTransform:
    offset_x: float
    offset_y: float
    units_per_degree: float = 100_000.0

    def __post_init__(self):
        values = [self.offset_x, self.offset_y, self.units_per_degree]
        if not np.isfinite(values).all() or self.units_per_degree <= 0:
            raise ValueError("Transform parameters must be finite and scale positive.")

    def to_wgs84(self, frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        if frame.crs is None or not frame.crs.is_engineering:
            raise ValueError("Expected local engineering coordinates; refusing double conversion.")
        geometry = shapely.transform(
            frame.geometry.to_numpy(),
            lambda xy: (xy + [self.offset_x, self.offset_y]) / self.units_per_degree,
        )
        result = gpd.GeoDataFrame(frame.drop(columns="geometry"), geometry=geometry, crs=4326)
        bounds = result.total_bounds
        if not np.isfinite(bounds).all() or (
            bounds[0] < -180 or bounds[2] > 180 or bounds[1] < -90 or bounds[3] > 90
        ):
            raise ValueError("Transformation produced invalid WGS84 bounds.")
        return result

    def to_local_geometry(self, geometry):
        return shapely.transform(
            geometry,
            lambda xy: xy * self.units_per_degree - [self.offset_x, self.offset_y],
        )


def road_segments(frame: gpd.GeoDataFrame, scale: float, prefix: str) -> pd.DataFrame:
    """Fingerprint undirected segments, retaining both endpoints and source identities."""
    parts = frame.explode(index_parts=False).reset_index(drop=True)
    if not parts.geometry.geom_type.eq("LineString").all():
        raise ValueError("Only line geometries can supply calibration segments.")
    coords, owners = shapely.get_coordinates(parts.geometry, return_index=True)
    delta = np.diff(coords, axis=0) * scale
    length = np.linalg.norm(delta, axis=1)
    keep = (np.diff(owners) == 0) & (length >= 50) & (length <= 1500)
    starts = coords[:-1][keep] * scale
    delta = delta[keep]
    reverse = (delta[:, 0] < 0) | ((delta[:, 0] == 0) & (delta[:, 1] < 0))
    starts[reverse] += delta[reverse]
    delta[reverse] *= -1
    owner = owners[:-1][keep]
    ids = parts["id"].astype(str).to_numpy() if "id" in parts else parts.index.astype(str)
    return pd.DataFrame(
        {
            "vx": np.round(delta[:, 0], 1),
            "vy": np.round(delta[:, 1], 1),
            "x": starts[:, 0],
            "y": starts[:, 1],
            "end_x": starts[:, 0] + delta[:, 0],
            "end_y": starts[:, 1] + delta[:, 1],
            "source_id": [f"{prefix}:{ids[i]}" for i in owner],
            "segment_id": np.flatnonzero(keep),
        }
    )


def calibrate_geographic_model(local_roads, reference_roads, units_per_degree=100_000.0):
    """Find a strongly supported translation after an explicit degree-scale hypothesis.

    A modal start-point translation seeds candidate controls. Independent end points
    reject equal-length parallel segments that are not corresponding features.
    Geographic tiles reserve spatial validation controls before estimating offsets.
    """
    if local_roads.crs is None or not local_roads.crs.is_engineering:
        raise ValueError("Local roads need an engineering CRS.")
    reference_roads = reference_roads.to_crs(4326)
    left = road_segments(local_roads, 1.0, "rhino")
    right = road_segments(reference_roads, units_per_degree, "reference")
    pairs = left.merge(right, on=["vx", "vy"], suffixes=("_local", "_reference"))
    if pairs.empty:
        raise ValueError("No matching segment vectors; the coordinate hypothesis is unsupported.")
    offsets = (
        pairs[["x_reference", "y_reference"]].to_numpy() - pairs[["x_local", "y_local"]].to_numpy()
    )
    mode = pd.DataFrame(offsets).round(3).value_counts().index[0]
    seed = np.asarray(mode)
    start_error = np.linalg.norm(offsets - seed, axis=1)
    end_offsets = (
        pairs[["end_x_reference", "end_y_reference"]].to_numpy()
        - pairs[["end_x_local", "end_y_local"]].to_numpy()
    )
    keep = (start_error < 0.02) & (np.linalg.norm(end_offsets - seed, axis=1) < 0.02)
    controls = pairs.loc[keep].copy()
    controls = controls.drop_duplicates("segment_id_local").drop_duplicates("segment_id_reference")
    if len(controls) < 100 or controls.source_id_local.nunique() < 20:
        raise ValueError("Insufficient independent road objects for calibration.")
    # Hold out entire ~0.02 degree spatial tiles, not random vertices of the same line.
    tile_x = np.floor(controls.x_reference / (units_per_degree * 0.02)).astype(int)
    tile_y = np.floor(controls.y_reference / (units_per_degree * 0.02)).astype(int)
    controls["partition"] = np.where((tile_x + 3 * tile_y) % 5 == 0, "validation", "fit")
    fit = controls[controls.partition == "fit"]
    validation = controls[controls.partition == "validation"]
    if len(fit) < 50 or len(validation) < 20:
        raise ValueError("Insufficient spatially held-out controls.")
    offsets = (
        fit[["x_reference", "y_reference"]].to_numpy() - fit[["x_local", "y_local"]].to_numpy()
    )
    offset_x, offset_y = np.median(offsets, axis=0)
    transform = GeographicModelTransform(float(offset_x), float(offset_y), units_per_degree)
    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    for endpoint in ["", "end_"]:
        px = (controls[f"{endpoint}x_local"] + offset_x) / units_per_degree
        py = (controls[f"{endpoint}y_local"] + offset_y) / units_per_degree
        rx = controls[f"{endpoint}x_reference"] / units_per_degree
        ry = controls[f"{endpoint}y_reference"] / units_per_degree
        _, _, errors = geod.inv(px.to_numpy(), py.to_numpy(), rx.to_numpy(), ry.to_numpy())
        controls[f"{endpoint}residual_m"] = errors
    if controls[["residual_m", "end_residual_m"]].max().max() > 0.05:
        raise ValueError("Reference reconstruction exceeds the 5 cm numerical check.")
    return transform, controls, {"candidate_pairs": len(pairs), "accepted_segments": len(controls)}
