"""Export a typed property graph without inventing access or ownership relations."""

import json
import math
from importlib.metadata import version
from pathlib import Path

import pandas as pd
import shapely
from shapely.geometry import Point

from .config import Settings
from .data import Layers, utc_now, write_json
from .metrics import building_neighbors

NODE_LABELS = {"StudyArea", "Building", "StreetNode", "StreetSegment"}
RELATION_TYPES = {
    "PART_OF_DATASET",
    "NEAR_GEOMETRY",
    "NEAR_STREET",
    "STARTS_AT",
    "ENDS_AT",
    "CONNECTS_TO",
}


def clean_properties(properties: dict) -> dict:
    result = {}
    for key, value in properties.items():
        if hasattr(value, "item"):
            value = value.item()
        if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
            continue
        if isinstance(value, float) and not math.isfinite(value):
            continue
        if isinstance(value, (dict, list, tuple)):
            value = json.dumps(value, ensure_ascii=False)
        if not isinstance(value, (str, bool, float, int)):
            value = str(value)
        result[str(key)] = value
    return result


def uid(study_area_id: str, label: str, source_id: str) -> str:
    return f"{study_area_id}::{label}::{source_id}"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")


def export_graph(settings: Settings) -> dict:
    layers = Layers.load(settings.data_dir / "processed")
    summary_path = settings.data_dir / "processed/summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary["study_area_id"] != settings.study_area_id:
        raise ValueError("配置研究区与已计算数据不一致；请使用对应的配置。")
    requested = {
        "building_neighbor_distance_m": settings.building_neighbor_distance_m,
        "nearest_street_max_distance_m": settings.nearest_street_max_distance_m,
    }
    if requested != summary["parameters"]:
        raise ValueError("关系阈值与已计算数据不同；请先重新运行 analyze。")
    directory = settings.data_dir / "graph"
    directory.mkdir(parents=True, exist_ok=True)
    nodes, relationships = [], []
    area_uid = uid(settings.study_area_id, "StudyArea", settings.study_area_id)
    source = summary["source_manifest"].get("source", "unknown")
    metric_crs = summary["metric_crs"]

    def add_nodes(label, frame):
        centers = frame.geometry.centroid.to_crs(4326)
        for i, row in frame.iterrows():
            identifier = uid(settings.study_area_id, label, str(row.id))
            properties = row.drop(labels=[frame.geometry.name]).to_dict()
            properties.update(
                {
                    "source_id": str(row.id),
                    "study_area_id": settings.study_area_id,
                    "source": row.get("source", source),
                    "geometry_wkt": row.geometry.wkt,
                    "geometry_crs": metric_crs,
                    "longitude": centers.loc[i].x,
                    "latitude": centers.loc[i].y,
                }
            )
            nodes.append(
                {"uid": identifier, "label": label, "properties": clean_properties(properties)}
            )
            if label != "StudyArea":
                add_relation("PART_OF_DATASET", identifier, area_uid, {})

    def add_relation(kind, start, end, properties, discriminator=""):
        relationships.append(
            {
                "uid": f"{start}|{kind}|{end}|{discriminator}",
                "type": kind,
                "source": start,
                "target": end,
                "properties": clean_properties(
                    {**properties, "study_area_id": settings.study_area_id}
                ),
            }
        )

    add_nodes("StudyArea", layers.area)
    add_nodes("Building", layers.buildings)
    add_nodes("StreetNode", layers.street_nodes)
    add_nodes("StreetSegment", layers.street_segments)
    for row in layers.street_segments.itertuples():
        segment = uid(settings.study_area_id, "StreetSegment", row.id)
        start = uid(settings.study_area_id, "StreetNode", row.u)
        end = uid(settings.study_area_id, "StreetNode", row.v)
        add_relation("STARTS_AT", segment, start, {})
        add_relation("ENDS_AT", segment, end, {})
        add_relation(
            "CONNECTS_TO",
            start,
            end,
            {
                "segment_id": row.id,
                "length_m": row.length_m,
                "undirected": True,
                "legal_routing": False,
            },
            discriminator=row.id,
        )
    for row in layers.buildings.itertuples():
        if pd.notna(row.nearest_street_id):
            add_relation(
                "NEAR_STREET",
                uid(settings.study_area_id, "Building", row.id),
                uid(settings.study_area_id, "StreetSegment", row.nearest_street_id),
                {"distance_m": row.nearest_street_distance_m, "access_verified": False},
            )
    for pair in building_neighbors(layers.buildings, settings.building_neighbor_distance_m):
        add_relation(
            "NEAR_GEOMETRY",
            uid(settings.study_area_id, "Building", pair["source_id"]),
            uid(settings.study_area_id, "Building", pair["target_id"]),
            {"distance_m": pair["distance_m"], "undirected": True},
        )
    _write_jsonl(directory / "nodes.jsonl", nodes)
    _write_jsonl(directory / "relationships.jsonl", relationships)
    metadata = {
        "schema_version": 1,
        "study_area_id": settings.study_area_id,
        "exported_at_utc": utc_now(),
        "metric_crs": metric_crs,
        "nodes": len(nodes),
        "relationships": len(relationships),
        "parameters": summary["parameters"],
        "source_manifest": summary["source_manifest"],
        "require_topology_validation": bool(
            summary["source_manifest"].get("require_topology_validation", False)
        ),
        "software": {
            name: version(name) for name in ["geopandas", "osmnx", "momepy", "shapely", "neo4j"]
        },
    }
    write_json(directory / "metadata.json", metadata)
    validate_graph(directory)
    return metadata


def validate_graph(directory: Path) -> tuple[dict, list[dict], list[dict]]:
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    if metadata.get("schema_version") != 1:
        raise ValueError("不支持的图文件版本。")
    identifier = metadata.get("study_area_id")
    if not identifier:
        raise ValueError("图文件缺少 study_area_id。")

    def read(name):
        records = []
        for line_number, line in enumerate(
            (directory / name).read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise ValueError(f"{name}:{line_number} 不是有效 JSON。") from error
        return records

    nodes, relationships = read("nodes.jsonl"), read("relationships.jsonl")
    node_ids = set()
    for row in nodes:
        if row.get("label") not in NODE_LABELS:
            raise ValueError("节点类型不在允许列表中。")
        key = row.get("uid")
        if not isinstance(key, str) or not key or key in node_ids:
            raise ValueError("节点 uid 为空或重复。")
        node_ids.add(key)
        properties = row.get("properties")
        if not isinstance(properties, dict) or "uid" in properties:
            raise ValueError("节点 properties 必须是映射，且不能覆盖保留的 uid。")
        if properties.get("study_area_id") != identifier:
            raise ValueError("节点研究区与元数据不一致。")
        for field, lower, upper in [("longitude", -180, 180), ("latitude", -90, 90)]:
            value = properties.get(field)
            if not isinstance(value, (int, float)) or not lower <= value <= upper:
                raise ValueError(f"节点 {field} 必须是有效 WGS84 数值。")
        if any(not isinstance(value, (str, bool, int, float)) for value in properties.values()):
            raise ValueError("节点属性必须是已规范化的标量。")
    relationship_ids = set()
    for row in relationships:
        if row.get("type") not in RELATION_TYPES:
            raise ValueError("关系类型不在允许列表中。")
        key = row.get("uid")
        if not isinstance(key, str) or not key or key in relationship_ids:
            raise ValueError("关系 uid 为空或重复。")
        relationship_ids.add(key)
        if row.get("source") not in node_ids or row.get("target") not in node_ids:
            raise ValueError("关系引用了不存在的节点。")
        properties = row.get("properties")
        if not isinstance(properties, dict) or "uid" in properties:
            raise ValueError("关系属性不能覆盖保留的 uid。")
        if properties.get("study_area_id") != identifier:
            raise ValueError("关系研究区与元数据不一致。")
        if any(not isinstance(value, (str, bool, int, float)) for value in properties.values()):
            raise ValueError("关系属性必须是已规范化的标量。")
    if metadata.get("nodes") != len(nodes) or metadata.get("relationships") != len(relationships):
        raise ValueError("图文件行数与 metadata 记录不一致。")
    for row in nodes + relationships:
        if any(
            isinstance(value, float) and not math.isfinite(value)
            for value in row["properties"].values()
        ):
            raise ValueError("图属性含非有限数值。")
    if metadata.get("require_topology_validation"):
        _validate_topology(nodes, relationships, metadata["metric_crs"])
    return metadata, nodes, relationships


def _validate_topology(nodes, relationships, crs):
    """Independent checks at the import boundary, before any database mutation."""
    by_id = {row["uid"]: row for row in nodes}
    geometry = {}
    expected_types = {
        "StudyArea": {"Polygon", "MultiPolygon"},
        "Building": {"Polygon", "MultiPolygon"},
        "StreetNode": {"Point"},
        "StreetSegment": {"LineString"},
    }
    for key, row in by_id.items():
        props = row["properties"]
        geom = shapely.from_wkt(props.get("geometry_wkt", ""))
        if geom is None or geom.is_empty or not geom.is_valid:
            raise ValueError("图节点几何无效。")
        if props.get("geometry_crs") != crs or geom.geom_type not in expected_types[row["label"]]:
            raise ValueError("图节点几何类型或 CRS 不一致。")
        geometry[key] = geom
    starts, ends, connections = {}, {}, {}
    for relation in relationships:
        kind, start, end = relation["type"], relation["source"], relation["target"]
        labels = by_id[start]["label"], by_id[end]["label"]
        if kind in {"STARTS_AT", "ENDS_AT"}:
            if labels != ("StreetSegment", "StreetNode"):
                raise ValueError("街段端点关系类型错误。")
            target = starts if kind == "STARTS_AT" else ends
            if start in target:
                raise ValueError("街段含重复端点关系。")
            target[start] = end
        elif kind == "CONNECTS_TO":
            if labels != ("StreetNode", "StreetNode"):
                raise ValueError("道路连接关系类型错误。")
            segment = relation["properties"].get("segment_id")
            if segment in connections:
                raise ValueError("街段投影含重复连接。")
            connections[segment] = relation
        elif kind == "NEAR_STREET":
            if labels != ("Building", "StreetSegment"):
                raise ValueError("建筑街道邻近关系类型错误。")
            distance = relation["properties"].get("distance_m", -1)
            if distance < 0 or not math.isclose(
                geometry[start].distance(geometry[end]), distance, abs_tol=0.02
            ):
                raise ValueError("建筑街道邻近距离与几何不符。")
    for key, row in by_id.items():
        if row["label"] != "StreetSegment":
            continue
        if key not in starts or key not in ends:
            raise ValueError("街段缺少端点关系。")
        line = geometry[key]
        length = row["properties"].get("length_m", -1)
        if length <= 0 or not math.isclose(length, line.length, abs_tol=0.02):
            raise ValueError("街段权重与几何长度不符。")
        if (
            max(
                Point(line.coords[0]).distance(geometry[starts[key]]),
                Point(line.coords[-1]).distance(geometry[ends[key]]),
            )
            > 0.02
        ):
            raise ValueError("街段几何与拓扑端点不符。")
        connection = connections.get(row["properties"]["source_id"])
        if (
            connection is None
            or connection["source"] != starts[key]
            or connection["target"] != ends[key]
            or not math.isclose(connection["properties"].get("length_m", -1), length, abs_tol=0.02)
        ):
            raise ValueError("街段连接投影与端点或长度不一致。")
