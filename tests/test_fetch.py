"""Actual OSMnx conversion with a tiny mocked download: reverse edges count once."""

import geopandas as gpd
import networkx as nx
import pandas as pd
from shapely.geometry import Point, Polygon

from urban_fabric.config import Settings
from urban_fabric.fetch import fetch_osm


def test_osm_conversion_deduplicates_reverse_edges(tmp_path, monkeypatch):
    features = gpd.GeoDataFrame(
        {"building": ["yes", "yes"], "height": ["8", None]},
        index=pd.MultiIndex.from_tuples([("way", 101), ("node", 999)]),
        geometry=[
            Polygon([(118.01, 32.01), (118.011, 32.01), (118.011, 32.011), (118.01, 32.011)]),
            Point(118.02, 32.02),
        ],
        crs=4326,
    )
    graph = nx.MultiDiGraph(crs="EPSG:4326", simplified=True)
    graph.add_node(1, x=118.01, y=32.01)
    graph.add_node(2, x=118.02, y=32.01)
    graph.add_edge(1, 2, osmid=10, length=1000, highway="residential")
    graph.add_edge(2, 1, osmid=10, length=1000, highway="residential")
    monkeypatch.setattr("urban_fabric.fetch.ox.features_from_polygon", lambda *a, **k: features)
    monkeypatch.setattr("urban_fabric.fetch.ox.graph_from_polygon", lambda *a, **k: graph)
    settings = Settings("test_area", tmp_path, bbox=(118, 32, 118.1, 32.1))
    layers = fetch_osm(settings)
    assert len(layers.buildings) == 1
    assert layers.buildings.id.iloc[0] == "osm:way:101"
    assert len(layers.street_segments) == 1
    assert set(layers.street_nodes.id) == {"osm:node:1", "osm:node:2"}
    assert (tmp_path / "raw/osm_network.graphml").exists()
