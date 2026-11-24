"""Deterministic synthetic fixtures. Never represented as observations of a real city."""

from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, Point, box

from .config import Settings
from .data import Layers, utc_now, write_json


def make_demo(output: Path) -> Settings:
    crs = "EPSG:32650"
    x, y = 500_000, 3_540_000
    area = gpd.GeoDataFrame(
        {"id": ["synthetic_demo"]}, geometry=[box(x, y, x + 400, y + 400)], crs=crs
    )
    buildings = gpd.GeoDataFrame(
        {
            "id": [f"demo:building:{i}" for i in range(6)],
            "height": ["12", "9 m", None, "15", "20 ft", "unknown"],
            "building_levels": ["4", "3", None, "5", "2", None],
            "building": ["yes"] * 6,
        },
        geometry=[
            box(x + 30, y + 30, x + 60, y + 50),
            box(x + 70, y + 30, x + 100, y + 50),
            box(x + 240, y + 30, x + 270, y + 70),
            box(x + 30, y + 240, x + 70, y + 270),
            box(x + 240, y + 240, x + 280, y + 280),
            box(x + 390, y + 300, x + 420, y + 320),
        ],
        crs=crs,
    )
    coordinates = [(0, 200), (200, 200), (400, 200), (200, 0), (200, 400)]
    nodes = gpd.GeoDataFrame(
        {"id": [f"demo:node:{i}" for i in range(5)]},
        geometry=[Point(x + a, y + b) for a, b in coordinates],
        crs=crs,
    )
    segments = gpd.GeoDataFrame(
        {
            "id": [f"demo:segment:{i}" for i in range(4)],
            "u": ["demo:node:1"] * 4,
            "v": [f"demo:node:{i}" for i in [0, 2, 3, 4]],
        },
        geometry=[
            LineString([nodes.geometry.iloc[1], nodes.geometry.iloc[i]]) for i in [0, 2, 3, 4]
        ],
        crs=crs,
    )
    layers = Layers(area, buildings, nodes, segments)
    layers.save(output / "raw")
    write_json(
        output / "raw/manifest.json",
        {
            "schema_version": 1,
            "study_area_id": "synthetic_demo",
            "source": "SYNTHETIC",
            "created_at_utc": utc_now(),
            "description": "Six rectangles and a cross-shaped network.",
            "expected_area_m2": 160_000,
            "expected_street_length_m": 800,
            "expected_intersections": 1,
        },
    )
    return Settings(
        "synthetic_demo", output, building_neighbor_distance_m=20, nearest_street_max_distance_m=180
    )
