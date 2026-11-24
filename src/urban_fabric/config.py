"""Configuration independent of the working directory."""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Settings:
    study_area_id: str
    data_dir: Path
    bbox: tuple[float, float, float, float] | None = None
    place: str | None = None
    polygon_path: Path | None = None
    network_type: str = "walk"
    timeout_seconds: int = 180
    metric_crs: str = "auto"
    building_neighbor_distance_m: float = 20.0
    nearest_street_max_distance_m: float = 100.0


def load_config(path: str | Path) -> Settings:
    path = Path(path).resolve()
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("study_area"), dict):
        raise ValueError("配置需要 study_area 映射。")
    area = doc["study_area"]
    identifier = str(area.get("id", ""))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", identifier):
        raise ValueError("study_area.id 需为 1–80 位字母、数字、下划线或连字符。")
    options = [area.get("bbox"), area.get("place"), area.get("polygon")]
    if sum(x is not None for x in options) != 1:
        raise ValueError("study_area 中 bbox、place、polygon 必须且只能设置一个。")
    bbox = None
    if area.get("bbox") is not None:
        values = area["bbox"]
        if not isinstance(values, (list, tuple)) or len(values) != 4:
            raise ValueError("bbox 顺序为 [west, south, east, north]。")
        bbox = tuple(float(x) for x in values)
        west, south, east, north = bbox
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError("bbox 必须是有效 WGS84 范围，且不跨越日期变更线。")
    if area.get("place") is not None and not str(area["place"]).strip():
        raise ValueError("place 不能是空字符串。")
    paths = doc.get("paths", {})
    metrics = doc.get("metrics", {})
    osm = doc.get("osm", {})
    neighbor = float(metrics.get("building_neighbor_distance_m", 20))
    nearest = float(metrics.get("nearest_street_max_distance_m", 100))
    if not (0 <= neighbor < float("inf") and 0 < nearest < float("inf")):
        raise ValueError("邻近阈值需有限且非负；最近街段阈值需大于零。")
    network_type = osm.get("network_type", "walk")
    if network_type not in {"walk", "bike", "drive", "all", "all_public"}:
        raise ValueError("不支持的 OSMnx network_type。")
    timeout = int(osm.get("timeout_seconds", 180))
    if timeout <= 0:
        raise ValueError("timeout_seconds 必须大于零。")
    return Settings(
        study_area_id=identifier,
        data_dir=(path.parent / paths.get("data_dir", "../data/" + identifier)).resolve(),
        bbox=bbox,
        place=area.get("place"),
        polygon_path=(path.parent / area["polygon"]).resolve() if area.get("polygon") else None,
        network_type=network_type,
        timeout_seconds=timeout,
        metric_crs=str(metrics.get("crs", "auto")),
        building_neighbor_distance_m=neighbor,
        nearest_street_max_distance_m=nearest,
    )
