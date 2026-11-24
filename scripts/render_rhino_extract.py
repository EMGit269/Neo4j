"""Static research map of local-coordinate Rhino candidate footprints and model heights."""

import argparse
import json
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import Normalize
from matplotlib.font_manager import FontProperties
from matplotlib.patches import Rectangle
from matplotlib.ticker import FuncFormatter
from shapely.geometry import box


def render(root, *, bounds_km=None, region_name="局部窗口", output_dir=None):
    buildings = gpd.read_parquet(root / "building_candidates.parquet")
    lines = gpd.read_parquet(root / "context_linework.parquet")
    report = json.loads((root / "report.json").read_text())
    font_path = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
    font = FontProperties(fname=font_path) if font_path.exists() else FontProperties()
    if font_path.exists():
        from matplotlib import font_manager

        font_manager.fontManager.addfont(str(font_path))
        plt.rcParams["font.family"] = font.get_name()
    plt.rcParams["axes.unicode_minus"] = False
    fig, axes = plt.subplots(1, 2, figsize=(16, 9), gridspec_kw={"width_ratios": [1.5, 1]})
    fig.set_facecolor("#f6f5ef")
    color_map, norm = "viridis", Normalize(3, 60, clip=True)
    if bounds_km is None:
        centers = buildings.geometry.centroid
        density, xbins, ybins = np.histogram2d(centers.x, centers.y, bins=25)
        xi, yi = np.unravel_index(density.argmax(), density.shape)
        xmid, ymid = (xbins[xi] + xbins[xi + 1]) / 2, (ybins[yi] + ybins[yi + 1]) / 2
        bounds = [xmid - 1500, ymid - 2000, xmid + 1500, ymid + 2000]
    else:
        bounds = [float(value) * 1000 for value in bounds_km]
    west, south, east, north = bounds
    if not (np.isfinite(bounds).all() and west < east and south < north):
        raise ValueError("Window must be [xmin, ymin, xmax, ymax] in local km.")
    window = box(*bounds)
    detail = buildings[buildings.intersects(window)]
    for axis in axes:
        axis.set_facecolor("#f6f5ef")
        axis.tick_params(labelsize=9, colors="#60736b")
        axis.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value / 1000:g}"))
        axis.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value / 1000:g}"))
        axis.set_xlabel("局部 X / 千模型单位", fontproperties=font, color="#60736b")
        axis.set_ylabel("局部 Y / 千模型单位", fontproperties=font, color="#60736b")
        for spine in axis.spines.values():
            spine.set_edgecolor("#d5dcd2")
    buildings.plot(
        ax=axes[0],
        column="height_extent_m",
        cmap=color_map,
        norm=norm,
        linewidth=0,
        rasterized=True,
    )
    axes[0].add_patch(
        Rectangle(
            (west, south),
            east - west,
            north - south,
            fill=False,
            edgecolor="#b76540",
            linewidth=1.3,
        )
    )
    axes[0].set_title(
        f"全域提取结果 · 橙框为{region_name}", fontproperties=font, loc="left", pad=12
    )
    for role, color, width in [
        ("road_linework", "#aebbb8", 0.4),
        ("water_linework", "#72a6b3", 0.6),
        ("railway_linework", "#b77858", 0.7),
    ]:
        selected = lines[(lines.role == role) & lines.intersects(window)]
        if len(selected):
            selected.plot(ax=axes[1], color=color, linewidth=width)
    detail.plot(
        ax=axes[1],
        column="height_extent_m",
        cmap=color_map,
        norm=norm,
        linewidth=0.2,
        edgecolor="#faf9f3",
        rasterized=True,
    )
    axes[1].set_xlim(west, east)
    axes[1].set_ylim(south, north)
    axes[1].set_title(
        f"{region_name} · {(east - west) / 1000:g} × {(north - south) / 1000:g} 千模型单位",
        fontproperties=font,
        loc="left",
        pad=12,
    )
    bar = fig.colorbar(
        plt.cm.ScalarMappable(norm=norm, cmap=color_map),
        ax=axes,
        orientation="horizontal",
        fraction=0.035,
        pad=0.12,
        shrink=0.6,
        extend="max",
    )
    bar.set_label("模型竖向高度 / m（60 m 以上统一显示；未经实测核验）", fontproperties=font)
    fig.suptitle(
        "苏州城市模型：建筑几何提取核验", fontproperties=font, fontsize=22, x=0.07, ha="left"
    )
    fig.text(
        0.07,
        0.9,
        f"{len(buildings):,} 个几何候选单元 · 不是已核验建筑栋数 · "
        f"窗口内相交候选 {len(detail):,} 个 · "
        f"X {west / 1000:g}–{east / 1000:g} / Y {south / 1000:g}–{north / 1000:g} 千模型单位",
        fontproperties=font,
        fontsize=11,
        color="#52675e",
    )
    fig.text(
        0.07,
        0.04,
        "本图保留原模型坐标，平面单位不保证为地面米。实际位置与物理指标见地理配准导出。"
        "模型高度来源与采集年份未确认；道路线尚未认定为可路由中心线。",
        fontproperties=font,
        fontsize=10,
        color="#52675e",
    )
    fig.subplots_adjust(left=0.07, right=0.95, top=0.85, bottom=0.24, wspace=0.23)
    preview = output_dir or root / "preview"
    preview.mkdir(exist_ok=True, parents=True)
    fig.savefig(preview / "footprints_heights.png", dpi=160, facecolor=fig.get_facecolor())
    plt.close(fig)
    from urban_fabric.data import write_json

    write_json(
        preview / "window.json",
        {
            "region_name": region_name,
            "bbox_local_m": bounds,
            "bbox_local_km": [value / 1000 for value in bounds],
            "axis_units": "1000 model units; legacy km field names do not imply ground km",
            "detail_candidates": len(detail),
            "source_sha256": report["source_sha256"],
        },
    )
    print(
        json.dumps(
            {
                "preview": str(preview / "footprints_heights.png"),
                "detail_candidates": len(detail),
                "source_sha256": report["source_sha256"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument(
        "--window-km", type=float, nargs=4, metavar=("XMIN", "YMIN", "XMAX", "YMAX")
    )
    parser.add_argument("--region-name", default="局部窗口")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    render(
        args.data_dir.resolve(),
        bounds_km=args.window_km,
        region_name=args.region_name,
        output_dir=args.output_dir.resolve() if args.output_dir else None,
    )
