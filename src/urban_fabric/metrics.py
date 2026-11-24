"""Metric geometry only: geographic degrees must never become area or distance units."""

import json
import math
import re

import geopandas as gpd
import momepy
import networkx as nx
import numpy as np
import pandas as pd
from pyproj import CRS
from shapely import make_valid
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.ops import unary_union

from .config import Settings
from .data import Layers, utc_now, write_json


def _polygon(geometry):
    if geometry is None or geometry.is_empty:
        return None
    geometry = make_valid(geometry)
    if isinstance(geometry, (Polygon, MultiPolygon)):
        return geometry
    if isinstance(geometry, GeometryCollection):
        parts = [_polygon(part) for part in geometry.geoms]
        parts = [part for part in parts if part is not None]
        return unary_union(parts) if parts else None
    return None


def parse_height(value) -> float | None:
    """OSM height with metres or feet. Ambiguous lists/ranges stay missing."""
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*(m|metres?|meters?|ft|feet|')?\s*", str(value), re.I
    )
    if not match:
        return None
    number = float(match.group(1))
    if match.group(2) and match.group(2).lower() in {"ft", "feet", "'"}:
        number *= 0.3048
    return number if math.isfinite(number) and number > 0 else None


def metric_crs(area: gpd.GeoDataFrame, requested: str) -> CRS:
    crs = area.to_crs(4326).estimate_utm_crs() if requested == "auto" else CRS(requested)
    if crs is None:
        raise ValueError("无法自动选择 UTM；请设置适合该研究区的米制投影。")
    crs = CRS(crs)
    if not crs.is_projected or not crs.axis_info:
        raise ValueError("metrics.crs 必须是米制投影；拒绝用经纬度计算面积、长度。")
    if any(not math.isclose(axis.unit_conversion_factor, 1) for axis in crs.axis_info[:2]):
        raise ValueError("metrics.crs 坐标单位必须是米。")
    return crs


def analyze(settings: Settings, *, retain_context: bool = False) -> tuple[Layers, dict]:
    raw = Layers.load(settings.data_dir / "raw")
    provenance = json.loads((settings.data_dir / "raw/manifest.json").read_text(encoding="utf-8"))
    if provenance.get("study_area_id") != settings.study_area_id:
        raise ValueError("raw 来源记录的研究区 ID 与配置不同。")
    if provenance.get("source") == "OpenStreetMap":
        expected_query = {
            "bbox_wsen": list(settings.bbox) if settings.bbox else None,
            "place": settings.place,
            "polygon_file": str(settings.polygon_path) if settings.polygon_path else None,
            "network_type": settings.network_type,
        }
        if provenance.get("query") != expected_query:
            raise ValueError("研究区或路网配置与 raw 查询记录不同；请先重新运行 fetch。")
    for name in ("area", "buildings", "street_nodes", "street_segments"):
        frame = getattr(raw, name)
        if frame.crs is None:
            raise ValueError(f"{name} 缺少 CRS。")
        if "id" not in frame or frame["id"].isna().any() or frame["id"].duplicated().any():
            raise ValueError(f"{name}.id 必须存在且唯一。")
    if len(raw.area) != 1 or raw.area.id.iloc[0] != settings.study_area_id:
        raise ValueError("area 应仅有一个对象，且 id 必须与配置的 study_area.id 相同。")
    retain_context = retain_context or bool(provenance.get("retain_context_network", False))
    crs = metric_crs(raw.area, settings.metric_crs)
    area = raw.area.to_crs(crs).copy()
    area.geometry = area.geometry.map(_polygon)
    if area.geometry.isna().any() or area.geometry.is_empty.any():
        raise ValueError("研究区没有有效面几何。")
    boundary = area.geometry.union_all()
    if boundary.area <= 0:
        raise ValueError("研究区面积必须大于零。")
    buildings = raw.buildings.to_crs(crs).copy()
    invalid_count = int((~buildings.geometry.is_valid).sum())
    buildings.geometry = buildings.geometry.map(_polygon)
    buildings = buildings[buildings.geometry.notna()].copy()
    buildings = buildings[~buildings.geometry.is_empty & (buildings.geometry.area > 0)].copy()
    buildings = buildings[buildings.intersects(boundary)].reset_index(drop=True)
    buildings["boundary_clipped"] = ~buildings.geometry.covered_by(boundary)
    # Measure whole footprints; only the area-wide coverage is clipped to the AOI.
    buildings["area_m2"] = buildings.geometry.area
    buildings["perimeter_m"] = buildings.geometry.length
    buildings["isoperimetric_quotient"] = (
        4 * np.pi * buildings.area_m2 / buildings.perimeter_m.pow(2)
    )
    if len(buildings):
        buildings["circular_compactness"] = momepy.circular_compactness(buildings.geometry)
        buildings["elongation"] = momepy.elongation(buildings.geometry)
        buildings["rectangularity"] = momepy.rectangularity(buildings.geometry)
    else:
        for column in ["circular_compactness", "elongation", "rectangularity"]:
            buildings[column] = pd.Series(dtype=float)
    parsed_height = pd.to_numeric(
        buildings.get("height", pd.Series(index=buildings.index, dtype=object)).map(parse_height),
        errors="coerce",
    )
    if "height_m" in buildings:
        existing_height = pd.to_numeric(buildings.height_m, errors="coerce")
        existing_height = existing_height.where(
            np.isfinite(existing_height) & (existing_height > 0)
        )
        parsed_height = parsed_height.fillna(existing_height)
    buildings["height_m"] = parsed_height
    buildings["levels"] = pd.to_numeric(
        buildings.get("building_levels", pd.Series(index=buildings.index, dtype=object)),
        errors="coerce",
    )
    buildings.loc[buildings.levels <= 0, "levels"] = np.nan
    segments = raw.street_segments.to_crs(crs).copy()
    segments = segments[segments.geometry.notna() & ~segments.geometry.is_empty].copy()
    segments = segments[segments.geometry.geom_type.isin(["LineString", "MultiLineString"])].copy()
    if not retain_context:
        segments = segments[segments.intersects(boundary)]
    segments = segments.reset_index(drop=True)
    segments["intersects_core"] = segments.intersects(boundary)
    nodes = raw.street_nodes.to_crs(crs).copy()
    endpoints = set(segments.u) | set(segments.v)
    nodes = nodes[nodes.id.isin(endpoints)].reset_index(drop=True)
    if endpoints - set(nodes.id):
        raise ValueError("道路数据缺少端点节点，无法生成一致的网络。")
    segments["length_m"] = segments.geometry.length
    segments["length_in_area_m"] = segments.geometry.intersection(boundary).length
    graph = nx.MultiGraph()
    graph.add_nodes_from(nodes.id)
    graph.add_edges_from((r.u, r.v) for r in segments.itertuples())
    graph = momepy.node_degree(graph, name="street_degree")
    nodes["street_degree"] = nodes.id.map(nx.get_node_attributes(graph, "street_degree")).astype(
        int
    )
    nodes["distinct_neighbor_count"] = nodes.id.map(
        {node: len(set(graph.neighbors(node)) - {node}) for node in graph}
    )
    nodes["within_area"] = nodes.geometry.covered_by(boundary)
    buildings["nearest_street_id"] = pd.Series([None] * len(buildings), dtype=object)
    buildings["nearest_street_distance_m"] = np.nan
    if len(buildings) and len(segments):
        joined = gpd.sjoin_nearest(
            buildings[["id", "geometry"]],
            segments[["id", "geometry"]].rename(columns={"id": "street_id"}),
            how="left",
            max_distance=settings.nearest_street_max_distance_m,
            distance_col="distance_m",
        )
        # Equidistant candidates get one reproducible tie break, not duplicate buildings.
        joined = joined.sort_values(["id", "distance_m", "street_id"], na_position="last")
        joined = joined.drop_duplicates("id").set_index("id")
        buildings["nearest_street_id"] = buildings.id.map(joined.street_id)
        buildings["nearest_street_distance_m"] = buildings.id.map(joined.distance_m)
    footprint_area = (
        buildings.geometry.union_all().intersection(boundary).area if len(buildings) else 0.0
    )
    area_km2 = boundary.area / 1_000_000
    intersections = int(((nodes.street_degree >= 3) & nodes.within_area).sum())
    warnings = []
    if invalid_count:
        warnings.append(f"{invalid_count} 个建筑几何经过 make_valid；请在图面复核。")
    if int(buildings.boundary_clipped.sum()):
        warnings.append("边界建筑保留完整轮廓计算形状，覆盖面积在 AOI 内裁切。")
    if not len(buildings):
        warnings.append("研究区没有可分析的有效建筑。")
    if buildings.nearest_street_id.isna().any():
        warnings.append("部分建筑在给定阈值内没有最近街段；没有补造通行关系。")
    summary = {
        "schema_version": 1,
        "study_area_id": settings.study_area_id,
        "computed_at_utc": utc_now(),
        "metric_crs": crs.to_string(),
        "source_manifest": provenance,
        "area_m2": float(boundary.area),
        "building_count": len(buildings),
        "building_footprint_union_in_area_m2": float(footprint_area),
        "building_coverage_ratio": float(footprint_area / boundary.area),
        "building_count_per_km2": len(buildings) / area_km2,
        "street_length_in_area_m": float(segments.length_in_area_m.sum()),
        "street_length_density_m_per_km2": float(segments.length_in_area_m.sum() / area_km2),
        "intersection_count": intersections,
        "intersection_count_basis": (
            "Core OSM nodes with physical multigraph degree >= 3; "
            "not a count of consolidated real-world junctions."
        ),
        "intersection_density_per_km2": intersections / area_km2,
        "street_segments": len(segments),
        "street_nodes": len(nodes),
        "retains_context_network": retain_context,
        "street_segments_intersecting_core": int(segments.intersects_core.sum()),
        "quality": {
            "invalid_building_inputs": invalid_count,
            "excluded_building_inputs": len(raw.buildings) - len(buildings),
            "boundary_buildings": int(buildings.boundary_clipped.sum()),
            "missing_height_count": int(buildings.height_m.isna().sum()),
            "buildings_without_near_street": int(buildings.nearest_street_id.isna().sum()),
        },
        "parameters": {
            "building_neighbor_distance_m": settings.building_neighbor_distance_m,
            "nearest_street_max_distance_m": settings.nearest_street_max_distance_m,
        },
        "warnings": warnings,
        "semantics": (
            "Physical morphology and geometric proximity, not use rights or accessibility."
        ),
    }
    result = Layers(area, buildings, nodes, segments)
    result.save(settings.data_dir / "processed")
    buildings.drop(columns="geometry").to_csv(
        settings.data_dir / "processed/building_metrics.csv", index=False
    )
    segments.drop(columns="geometry").to_csv(
        settings.data_dir / "processed/street_metrics.csv", index=False
    )
    write_json(settings.data_dir / "processed/summary.json", summary)
    return result, summary


def building_neighbors(buildings: gpd.GeoDataFrame, threshold: float) -> list[dict]:
    """Use a spatial index rather than all-pairs distance calculations."""
    if not len(buildings):
        return []
    candidates = buildings.sindex.query(buildings.geometry, predicate="dwithin", distance=threshold)
    pairs = []
    for left, right in zip(*candidates, strict=True):
        if left >= right:
            continue
        a, b = buildings.iloc[left], buildings.iloc[right]
        u, v = sorted((a.id, b.id))
        pairs.append(
            {"source_id": u, "target_id": v, "distance_m": float(a.geometry.distance(b.geometry))}
        )
    return sorted(pairs, key=lambda x: (x["source_id"], x["target_id"]))
