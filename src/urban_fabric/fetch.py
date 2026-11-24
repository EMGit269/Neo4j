"""OSM acquisition of building footprints and physical street centerlines."""

import json
from importlib.metadata import version

import geopandas as gpd
import osmnx as ox
from shapely.geometry import box

from .config import Settings
from .data import Layers, utc_now, write_json


def _tag(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, float) and value != value:
        return None
    return str(value)


def study_area(settings: Settings) -> gpd.GeoDataFrame:
    if settings.bbox:
        area = gpd.GeoDataFrame(geometry=[box(*settings.bbox)], crs=4326)
    elif settings.place:
        area = ox.geocode_to_gdf(settings.place).to_crs(4326)
    elif settings.polygon_path:
        area = gpd.read_file(settings.polygon_path)
        if area.crs is None:
            raise ValueError("AOI 文件必须声明 CRS。")
        area = area.to_crs(4326)
    else:
        raise ValueError("未配置研究区。")
    polygon = area.geometry.union_all()
    if polygon.is_empty or polygon.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("研究区必须是非空面几何。")
    return gpd.GeoDataFrame({"id": [settings.study_area_id]}, geometry=[polygon], crs=4326)


def fetch_street_network(settings: Settings, area: gpd.GeoDataFrame):
    """Fetch only streets, leaving an existing building dataset intact."""
    raw = settings.data_dir / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    ox.settings.use_cache = True
    ox.settings.cache_folder = settings.data_dir / "cache"
    ox.settings.requests_timeout = settings.timeout_seconds
    polygon = area.to_crs(4326).geometry.union_all()
    graph = ox.graph_from_polygon(
        polygon, network_type=settings.network_type, simplify=True, retain_all=True
    )
    ox.save_graphml(graph, raw / "osm_network.graphml")
    # Physical centerline network: do not count opposite directed copies twice.
    physical = ox.convert.to_undirected(graph)
    nodes, edges = ox.graph_to_gdfs(physical)
    node_rows = [
        {"id": f"osm:node:{i}", "osm_id": str(i), "geometry": r.geometry}
        for i, r in nodes.iterrows()
    ]
    edge_rows = []
    for (u, v, key), edge in edges.iterrows():
        left, right = sorted((int(u), int(v)))
        edge_rows.append(
            {
                "id": f"osm:segment:{left}:{right}:{key}",
                "u": f"osm:node:{left}",
                "v": f"osm:node:{right}",
                "osm_id": _tag(edge.get("osmid")),
                "highway": _tag(edge.get("highway")),
                "name": _tag(edge.get("name")),
                "source": "OpenStreetMap",
                "geometry": edge.geometry,
            }
        )
    return (
        gpd.GeoDataFrame(node_rows, crs=nodes.crs),
        gpd.GeoDataFrame(edge_rows, crs=edges.crs),
    )


def fetch_osm(settings: Settings) -> Layers:
    raw = settings.data_dir / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    ox.settings.use_cache = True
    ox.settings.cache_folder = settings.data_dir / "cache"
    ox.settings.requests_timeout = settings.timeout_seconds
    area = study_area(settings)
    polygon = area.geometry.iloc[0]
    try:
        features = ox.features_from_polygon(polygon, tags={"building": True})
    except ox._errors.InsufficientResponseError as error:
        raise ValueError("OSM 没有返回建筑；请检查覆盖、范围，或换一个小研究区。") from error
    rows = []
    for index, feature in features.iterrows():
        geometry = feature.geometry
        if geometry is None or geometry.is_empty:
            continue
        if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            continue
        element, osmid = index[0], index[1]
        row = {
            "id": f"osm:{element}:{osmid}",
            "osm_element": str(element),
            "osm_id": str(osmid),
            "geometry": geometry,
        }
        for source, target in [
            ("building", "building"),
            ("height", "height"),
            ("building:levels", "building_levels"),
            ("name", "name"),
            ("amenity", "amenity"),
            ("shop", "shop"),
            ("office", "office"),
        ]:
            row[target] = _tag(feature.get(source))
        rows.append(row)
    if not rows:
        raise ValueError("返回结果没有面状建筑，无法继续形态分析。")
    buildings = gpd.GeoDataFrame(rows, crs=features.crs).to_crs(4326)
    nodes, edges = fetch_street_network(settings, area)
    layers = Layers(area, buildings, nodes, edges)
    layers.save(raw)
    write_json(
        raw / "manifest.json",
        {
            "schema_version": 1,
            "study_area_id": settings.study_area_id,
            "source": "OpenStreetMap",
            "acquired_at_utc": utc_now(),
            "attribution": "© OpenStreetMap contributors",
            "license": "ODbL-1.0",
            "osmnx_version": version("osmnx"),
            "query": {
                "bbox_wsen": settings.bbox,
                "place": settings.place,
                "polygon_file": str(settings.polygon_path) if settings.polygon_path else None,
                "network_type": settings.network_type,
            },
            "counts": {
                "building_features_returned": len(features),
                "polygon_buildings": len(buildings),
                "street_nodes": len(nodes),
                "physical_street_segments": len(edges),
            },
            "network_semantics": (
                "Undirected physical centerlines; not legal routing or access rights."
            ),
        },
    )
    return layers
