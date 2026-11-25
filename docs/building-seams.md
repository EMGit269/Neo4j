# 苏州 Rhino 建筑分缝修复

**更新：2026-11-25。** 按用户要求，全模型重复网格证据支持的疑似分缝全部合并；
同一多部件轮廓内部的近接网格分缝也一起处理。合并对象的统一模型高度取源块最高值，
每个原始源块及其高度仍单独保留。输出是可追溯的建筑轮廓候选，不代表已实测核验的建筑实体。

## 截图中的漏项

此前截图定位的源组件 66791 / 69181 已合并，原始模型高度 6 m / 3 m，
整组高度现在记录为 **6 m**；每个组件的高度仍见 `height_parts.parquet`。

用户后来提供的另一处截图用建筑轮廓形态与地图做了位置匹配。截图中的分缝位于两个源对象自身的
MultiPolygon 内部，而不是两个候选记录之间，因此旧的跨对象接缝检测没有检查到。对应源组件为：

- `66753`：原 MultiPolygon 2 个近接部分，合并后成为一个 Polygon。
- `66768`：原 MultiPolygon 3 个近接部分，合并后成为一个 Polygon。

前后截图：[`missed_seams_sample_before_after.png`](../data/suzhou_old_city/repaired/missed_seams_sample_before_after.png)。
这是此次补充规则的一个例子；相同检查已扩展至全模型，而不是只修截图区域。

## 方法

实现位于 `src/urban_fabric/seam_repair.py`，执行入口为 `scripts/repair_building_seams.py`，
参数见 `configs/building-seam-repair.yaml`。

1. 在全模型候选几何中识别近共边及重复的规则网格线；用整个模型恢复跨研究区边界的同组组件。
2. 对网格线支持的候选边，无论外轮廓转角能否通过旧的连续性阈值，都按本次用户指示合并。
   角点接触、缺少重复网格证据的共墙不因此自动合并。
3. 将 MultiPolygon 展开为部件，检查同一源对象内部的近接共边；只有坐标位置得到重复网格证据支持的部分才合并。
4. 优先精确 union；存在模型舍入小缝时，限制在候选组内部做相互吸附或 0.05 模型单位精度 union。
   本次用户已指定全并，必要时采用局部小缝闭合；否则保留原有多部件轮廓，不移动周边建筑。
5. 每个合并组的 `height_extent_m` 取所有源块 `height_extent_m` 的最大值；
   `model_height_min_m/max_m`、`model_height_values_json` 及独立源块几何、高度均保留。
6. 轮廓修复后恢复 WGS84，转入配套建筑 SHP 的自定义米制投影，重算面积、周长与 momepy 形态指标。

网格与吸附容差使用 Rhino 原生 XY 坐标单位；不能把它们解释为地面米。用户要求“全部合并”，
因此本版接受所有重复网格支持的候选边，自动合并范围比旧版更宽。成果仍需要后续抽样对照影像或权威建筑数据，
以检查规则网格候选是否误连相邻建筑。

## 最新结果

| 范围 | 数量 / 结果 |
|---|---:|
| 全模型输入候选 | 111,347 |
| 全模型输出候选 | 106,573 |
| 全模型合并组 | 5,156 |
| 全模型接受的重复网格候选边 | 4,793 |
| 全模型 MultiPolygon 内部接缝合并事件 | 543 |
| 当前古城区窗口原始相交候选 | 22,700 |
| 纳入边界补齐的源候选 | 22,701 |
| 当前窗口输出轮廓 | **21,827** |
| 当前窗口合并对象 | **944**（854 个跨源组件组合并组、90 个内部轮廓合并对象）|
| 当前窗口源高度不同的合并组 | **841** |
| 重复网格疑似分缝待处理候选 | **0** |
| 原/新窗口覆盖面积对称差 | **27.241 m²** |

当前窗口覆盖面积由 8,883,903.917 m² 变为 8,883,906.955 m²，
面积净变化约 +3.038 m²；对称差占窗口建筑覆盖面积约 0.00031%。
所有源 ID 恰好归属一个输出候选，建筑 ID 唯一，输出几何有效，输出高度均等于源块最高高度。

## 高度口径

`height_extent_m` 是组内源块模型高度的最大值，不是已测量的整栋高度。
`height_conflict=true` 表示一个组包含不同源高度；这类组现在仍有单一高度值，采用最大值。
`estimated_prism_volume_m3` 使用完整轮廓面积乘该最大高度，是偏高的棱柱体积估计；
`source_parts_prism_estimate_sum_m3` 是源块各自棱柱体积估计的求和。两者口径不同，均不等同于实测建筑体积。
具体源块轮廓和高度见 `height_parts.parquet`、`source_membership.parquet`。

## 文件与复现

- `buildings.parquet` / `building_metrics.csv`：修复后的建筑候选与投影指标。
- `suzhou_old_city_repaired.gpkg`：`buildings`、`height_parts`、`merged_groups` 图层。
- `source_membership.csv/parquet`：源对象到输出候选的一对一映射。
- `height_parts.parquet`：未改写的源块轮廓及各自模型高度。
- `remaining_review.parquet/csv`：列结构保留；本次重复网格候选已全部并入。
- `all_model_pair_decisions.parquet`：候选边、决策、轮次和来源 ID。
- `all_model_merge_events.json`：跨源合并与 MultiPolygon 内部合并事件。
- `repair_report.json`：输入指纹、窗口/全域计数、高度策略、面积差及写入读回核验。
- `missed_seams_sample_before_after.png`：用户截图中漏项的修复前后对照。

原始 `.3dm`、全域提取、配准前建筑与高度源数据保持不变。POI 地图已默认读取本次修复后的建筑轮廓；
“标出已合并建筑”图层用于定位修复对象，待核图层在没有候选时不显示。不会提供“查看截图修复位置”按钮。

```bash
python scripts/repair_building_seams.py
python scripts/render_seam_repair.py
python scripts/render_osm_pois.py
python scripts/render_georeferenced_suzhou.py
```
