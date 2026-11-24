# 指标口径（v1）

地理数据先转换到米制 CRS。OSM 流程默认对 AOI 选择本地 UTM；经纬度或英尺坐标系不直接用于面积距离。
Rhino 原始提取保留模型坐标；苏州模型经核验为经纬度×100000 后平移，不能把声明单位当实地米。
当前建筑使用 `suzhou_old_city/repaired/` 内分缝修复后的真实投影指标；
`georeferenced/` 保留修复前数据，旧局部字段仅用于追溯。
投影和面积修正见 [坐标恢复说明](suzhou-georeferencing.md)。
单个 UTM 分区不适合整国、超大跨度或跨日期变更线分析，应分区研究并重新设计汇总方法。

## 建筑对象

只处理与 AOI 相交的有效建筑面，保留完整轮廓计算形状。`boundary_clipped=true` 表示建筑跨越研究区边界，名称是边界标记，**并未实际裁切用于形状指标的几何**。

| 字段 | 定义 | 单位/范围 |
|---|---|---|
| area_m2 | 完整建筑轮廓面积 | m² |
| perimeter_m | 完整轮廓边界长度，含洞边界 | m |
| isoperimetric_quotient | 4πA/P² | 无量纲，通常 (0,1] |
| circular_compactness | momepy：面积 / 最小外接圆面积 | 无量纲，通常 (0,1] |
| rectangularity | momepy：面积 / 最小旋转外接矩形面积 | 无量纲，通常 (0,1] |
| elongation | momepy：最小旋转外接矩形短边/长边 | 无量纲，通常 (0,1] |
| height_m | 可明确解析的 OSM height，支持米与英尺 | m，缺失不估算 |
| levels | 可数值化的 building:levels；不自动推算层高 | 层 |
| nearest_street_distance_m | 建筑面与街段中心线的最小平面距离 | m |

`circular_compactness` 与 `isoperimetric_quotient` **不是同一个公式**。
多部件轮廓按一个 OSM 对象计算；重叠的 relation/way 仍可能形成多个对象，建筑数量不是经过人工核验的独立实体数。
非面或无法修复为面的对象不进入建筑分析；`make_valid` 的修复数量写入质量报告。

## 道路和节点

OSMnx 简化后物理网络包含真实路口及部分端点，去掉反向行驶拷贝，保留不同几何的平行街段。
原始有向 GraphML 单独保存。第一版只输出物理中心线分析，不处理合法通行、转向和真实入口。

| 字段 | 定义 |
|---|---|
| length_m | 完整街段投影几何长度 |
| length_in_area_m | 街段与 AOI 相交部分长度 |
| street_degree | 物理多重图的度数，平行边分别计数，自环计两次；不是道路等级 |
| distinct_neighbor_count | 不同邻接节点数，不含节点自身 |
| within_area | 节点是否在 AOI 内或边界上 |

路口口径为 `street_degree >= 3` 且节点位于 AOI 内，是网络节点计数，未经真实交叉口合并。
平行边、边界提取、简化和缺失道路均会影响结果。

## 当前 Rhino 数据的特定字段

`height_extent_m` 为模型最高 Z 减最低 Z；`base_z_m`、`top_z_m` 保留模型标高。
`estimated_prism_volume_m3` 为底轮廓面积乘高度，不是精确网格体积、建筑面积或容积率。
`area_in_window_m2` 计窗口内部分；面积、周长和 momepy 形态指标使用完整候选底轮廓。
`footprint_requires_review` 标记分割歧义、重叠或几何问题；未标记不等于已实测核验。
规则分块修复后提供 `seam_merged`、`source_component_count` 和来源 ID 列表。
组内源块高度不同则 `height_conflict=true`，`height_extent_m` 与整组棱柱体积留空，
保留 `model_height_min_m/max_m` 与逐源块高度，不自动当作真实建筑高差。
地理配准导出中的 `length_m` 在真实投影下计算；`legacy_local_*` 仍是历史模型单位测量。
来源线尚未确认为可路由街段。

## 研究区汇总

| 字段 | 定义 |
|---|---|
| building_count | 与 AOI 相交的有效 OSM 建筑面对象数，含边界对象 |
| building_coverage_ratio | 建筑轮廓并集在 AOI 内的面积 / AOI 面积 |
| building_count_per_km2 | 上述对象数 / AOI 平方公里面积 |
| street_length_density_m_per_km2 | AOI 内物理中心线总长 / AOI 平方公里面积 |
| intersection_density_per_km2 | AOI 内满足口径的路口数 / AOI 平方公里面积 |

覆盖率使用并集，避免相交/重叠面重复累加；建筑计数没有因此合并。
它是建筑覆盖率，**不是容积率**。没有完整可靠层数时，不输出容积率。

## 关系阈值

- `building_neighbor_distance_m`：建筑面之间距离小于等于阈值则导出 `NEAR_GEOMETRY`；每对只输出一次。相触、相交也可能距离为零。
- `nearest_street_max_distance_m`：只在阈值内挑选一个最近街段；等距时按稳定街段 ID 排序。未找到则保持缺失。
- 最近街段由完整建筑面与线计算，不是由建筑重心计算。
- 阈值是可修改的实验参数，不是通用城市规律。修改后须重新运行 `analyze`，再导图。

OSM 的行政时间、标签更新时点和现实建成时间并不相同；记录获取日期并不构成真实历时数据。
