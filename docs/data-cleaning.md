# 当前数据清洗与构图边界

当前数据来源为苏州 Rhino 城市模型，分析窗口 X=23000–29000、Y=0–5000 模型单位。
完整提取步骤和真实检查结果见 [苏州模型说明](suzhou-rhino-model.md)。

## Rhino 与形态计算

- 用 rhino3dm 读取原始对象、图层与网格，排除配景块定义。
- 依据完全相同 XYZ 的完整共边拆分几何候选，不按 XY 距离吸附建筑。
- 最低水平面合并为候选底轮廓，最高与最低 Z 的差值作为模型竖向高度。
- 保留凹形、多面和孔洞；相接、多水平面及重叠对象标记待核，不能直接当作建筑栋数。
- 使用 momepy.circular_compactness、elongation、rectangularity；当前面积、周长和形态指标在地理配准后的真实投影下重算。
- 窗口建筑保持完整轮廓，另存 area_in_window_m2；区内覆盖率采用轮廓并集。
- 全域和窗口 GeoPackage 写入后读回核对数量、ID、面积与局部工程 CRS。
- 已按用户要求合并全部重复网格支持的疑似分缝，并检查 MultiPolygon 内部接缝；当前窗口 21,827 个轮廓、
  944 个候选含接缝合并，待处理疑似分缝为 0。每个源 ID 可追溯，原始高度保存在源块表。
  方法与结果见 [分缝修复](building-seams.md)。合并组取源块最高模型高度，非测绘值。

## 道路和地图配准

原始 CAD 折线可能混合道路边界、中心线和交通岛；Z=0 的线交叉不能证明同层互通。
已通过下载 SHP 的 66,981 条对应道路线段恢复模型地理位置，方法见 [坐标恢复说明](suzhou-georeferencing.md)。
模型 XY 是经纬度×100000 后平移，不是实际米制坐标；当前采用完整 SHP 自定义投影 WKT。
重投影后修复 22 个无效建筑候选，保留标记；没有据此消除原有分割待核标记。
在道路来源和几何语义核对前，保留来源折线和裁切版本，暂不生成真实通行图。

OSM 数据使用 OSMnx 保留节点身份、平行边、桥隧和通行属性；简化时保留语义节点。
momepy.close_gaps 会移动端点，remove_false_nodes 会移除二度节点，均需有修复依据后使用。
建筑小、狭长不等于错误，不能直接套用删除或合并规则。

## 通用图验收

图结构需要检查唯一 ID、有效非空几何、单位、端点存在和方向、有限正权重、长度一致性，
保留分量和真实平行边，再做文件往返及数据库回读核验。
几何邻近关系与通行关系分开；模型高度、来源高度和实测高度分开。
Neo4j 当前通用导入器要求 WGS84 中心点；苏州已恢复 WGS84，但尚未构建并导入新图。

官方依据：[OSMnx](https://osmnx.readthedocs.io/en/stable/user-reference.html)、
[momepy](https://docs.momepy.org/stable/api.html)、
[GeoPandas make_valid](https://geopandas.org/en/stable/docs/reference/api/geopandas.GeoSeries.make_valid.html)、
[rhino3dm](https://mcneel.github.io/rhino3dm/python/api/)。
既有官方文档摘录和论文资料保留在 outputs，南京阶段的实际结果见历史归档。
