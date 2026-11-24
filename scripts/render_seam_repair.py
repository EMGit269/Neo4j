"""Before/after figures and varied sample contact sheets for the actual repair groups."""

import json
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager


def main():
    root = Path("data/suzhou_old_city/repaired")
    font = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
    if font.exists():
        font_manager.fontManager.addfont(str(font))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(font)).get_name()
    source = gpd.read_parquet(root / "height_parts.parquet").to_crs(3857)
    repaired = gpd.read_parquet(root / "buildings.parquet").to_crs(3857)
    report = json.loads((root / "repair_report.json").read_text())
    screenshot = report["screenshot_group"][0]["building_id"]
    selected = repaired[repaired.id == screenshot].iloc[0]
    merged = repaired[repaired.seam_merged].copy()
    # Cover multi-part groups, small/large footprints and different outline shapes.
    sample_ids = [screenshot]
    sample_ids.extend(merged.sort_values("source_component_count", ascending=False).id.head(2))
    sample_ids.extend(merged.nsmallest(2, "area_m2").id)
    sample_ids.extend(merged.nlargest(2, "area_m2").id)
    sample_ids.extend(merged.nsmallest(2, "rectangularity").id)
    sample_ids = list(dict.fromkeys(sample_ids))

    def plot_pair(axes, row, labels=True):
        w, s, e, n = row.geometry.bounds
        pad = max(e - w, n - s) * 0.35
        original = source.cx[w - pad : e + pad, s - pad : n + pad]
        result = repaired.cx[w - pad : e + pad, s - pad : n + pad]
        for ax, frame in zip(axes, [original, result], strict=True):
            ax.set_facecolor("#f2f3ed")
            frame.plot(ax=ax, facecolor="#c9d2cb", edgecolor="#7f9185", linewidth=0.55)
            ax.set_xlim(w - pad, e + pad)
            ax.set_ylim(s - pad, n + pad)
            ax.set_aspect("equal")
            ax.set_xticks([])
            ax.set_yticks([])
        group = source[source.repaired_building_id == row.id]
        group.plot(ax=axes[0], facecolor="#b3c7be", edgecolor="#4b7065", linewidth=1.0)
        gpd.GeoSeries([row.geometry], crs=repaired.crs).plot(
            ax=axes[1], facecolor="#9fc0b0", edgecolor="#3d6a58", linewidth=1.0
        )
        if labels:
            axes[0].set_title("合并前 · 源模型分缝", loc="left", fontsize=13)
            axes[1].set_title("合并后 · 完整候选轮廓", loc="left", fontsize=13)

    fig, axes = plt.subplots(1, 2, figsize=(11, 6))
    fig.set_facecolor("#f6f5ef")
    plot_pair(axes, selected)
    fig.suptitle("截图建筑的分缝修复", x=0.06, ha="left", fontsize=21)
    fig.text(
        0.06,
        0.07,
        "两块已合并为一个完整轮廓；保留原始 ID、6 m / 3 m 的模型高度，未虚构统一高度。",
        fontsize=10,
        color="#426057",
    )
    fig.subplots_adjust(top=0.84, bottom=0.13, left=0.05, right=0.98, wspace=0.13)
    fig.savefig(root / "screenshot_before_after.png", dpi=165, facecolor=fig.get_facecolor())
    plt.close(fig)

    fig, axes = plt.subplots(len(sample_ids), 2, figsize=(9, 3.0 * len(sample_ids)))
    for row_axes, uid in zip(axes, sample_ids, strict=True):
        row = repaired[repaired.id == uid].iloc[0]
        plot_pair(row_axes, row, labels=False)
        row_axes[0].set_title(f"合并前 · {row.source_component_count} 块", fontsize=10)
        row_axes[1].set_title(
            f"合并后 · {row.area_m2:.1f} m² · 高度{'冲突' if row.height_conflict else '一致'}",
            fontsize=10,
        )
    fig.tight_layout()
    fig.savefig(root / "repair_sample_contact_sheet.png", dpi=135)
    plt.close(fig)


if __name__ == "__main__":
    main()
