"""Small, explicit GeoParquet interchange format for each pipeline stage."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


@dataclass
class Layers:
    area: gpd.GeoDataFrame
    buildings: gpd.GeoDataFrame
    street_nodes: gpd.GeoDataFrame
    street_segments: gpd.GeoDataFrame

    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        for name in ("area", "buildings", "street_nodes", "street_segments"):
            frame = getattr(self, name)
            if frame.crs is None:
                raise ValueError(f"{name} 缺少 CRS，拒绝保存未定位的几何。")
            frame.to_parquet(directory / f"{name}.parquet", index=False)

    @classmethod
    def load(cls, directory: Path) -> "Layers":
        names = ("area", "buildings", "street_nodes", "street_segments")
        missing = [name for name in names if not (directory / f"{name}.parquet").exists()]
        if missing:
            raise ValueError(f"{directory} 缺少 {', '.join(missing)}；请先运行 fetch 或 demo。")
        return cls(**{name: gpd.read_parquet(directory / f"{name}.parquet") for name in names})
