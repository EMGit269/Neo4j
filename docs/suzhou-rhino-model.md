# 苏州 Rhino 城市模型读取与提取

**2026-11-24 坐标更正：** 已用下载 SHP 恢复真实地理位置。模型 XY 为经纬度×100000 后平移，
原“局部米制”面积不能作为实地指标。保留相同选框后投影面积为 31.6386 km²，
当前应使用 `data/suzhou_old_city/georeferenced/`，详见 [坐标恢复与指标重算](suzhou-georeferencing.md)。
以下提取记录中的局部 `_m` 字段和 30 km² 是原模型坐标口径。

**分缝修复更新：** 原窗口 22,700 个候选经规则分缝合并后，当前为 **22,073 个候选轮廓**，
使用 `data/suzhou_old_city/repaired/`。原提取与配准目录保留作为修复前记录，
源块高度冲突以范围和原始分块属性保留；见 [分缝修复](building-seams.md)。

核验日期：2026-11-24。输入：`source_files/苏州城市模型.3dm`，115,309,193 字节。
原始文件只读，未修改。独立输出目录：`data/suzhou_rhino_model/`。

## 已实际确认

模型为 Rhino 7 格式，声明单位为米，包含 29 个图层、54,937 个对象。
建筑图层只有一个合并网格：5,808,474 个顶点、1,987,451 个面，不是逐栋独立对象。
采用 rhino3dm 8.35.0 读取，不需要启动 Rhino；当前官方网页已介绍 9.2 beta，
本实现和测试按实际安装的 8.35 API 编写，没有直接套用新版接口名。

| 信息 | 本次结果 |
|---|---:|
| 按三维完整共边拆分的几何候选单元 | 111,347 |
| 具有可提取底部轮廓及正数竖向高度的候选单元 | 111,347 |
| 模型高度范围 | 3–207 m |
| 模型高度中位数 / 95 分位 | 9 / 36 m |
| 道路图层折线（原图层名 raod） | 44,672 |
| 水系折线 | 2,446 |
| 绿地类闭合线（原图层名 natrue） | 6,021 |
| 铁路折线 | 979 |

来源 SHA-256：`bd738f10b14c7758bedc7e004931695273d6dc402fd44b4effdea1599714a2a4`。
模型文件记录的创建和最后编辑时间均为 2022-06-04，**不能当作数据采集日期**。
苏州实际地理位置已通过 OSM SHP 道路对应关系恢复；来源机构和采集年份仍未确认。

## 如何从模型提取

1. 按图层识别建筑 Mesh 和场地折线，排除块定义中的人物、标志等配景几何。
2. 合并完全相同 XYZ 坐标对应的网格顶点，再按**三维完整共边**建立面邻接图。
   不按 XY 邻近吸附，不因为角点接触而合并；完整共边相接的建筑仍可能成为同一候选单元。
3. 对每个连通单元计算最低 Z、最高 Z 和差值，保存为 `base_z_m`、`top_z_m`、`height_extent_m`。
4. 合并位于最低 Z 的水平面，保留真实的凹形和孔洞，作为候选底轮廓；没有用外接矩形代替。
5. 在局部米制坐标下调用 momepy.circular_compactness、elongation、rectangularity，
   同时计算面积和周长。轮廓面积 × 高度只称为棱柱体积估算，不称为原模型精确体积。
6. 标记网格分割歧义、多水平层、多面底轮廓和显著轮廓重叠，保留对象以供复核。

## 必须保留的研究限制

**高度来源不明。** 所有候选单元底标高均为 0，高度全部为 3 m 的整数倍。
这与按层数或统一层高拉伸的模型特征一致，但不能据此断定制作方法。
已统一标记 `height_is_survey_verified=false`；没有把模型高度写成测绘高度。

**原模型没有有效定位声明，但坐标关系已从外部恢复。** EarthAnchor 为 0/0；
原始提取保留局部工程坐标作为溯源，不能把声明单位当成地面米。
新的地理导出完成 WGS84 恢复、自定义米制投影转换和形态指标重算，验证范围见专门说明。

**候选单元不等于建筑栋数。** 19,448 个单元涉及多面共享网格边等分割歧义；其中还包含
12,106 个具有多个水平标高的单元和 1,266 个多面底轮廓单元。这些标记存在交集，不能相加。
检测到 14 对显著底轮廓重叠，涉及 26 个单元；综合待核轮廓标记为 19,471 个。
底轮廓阈值为重叠面积大于 max(1 m², 较小轮廓面积的 2%)。
没有因面积小、狭长或高度低而删除对象；1,139 个面积不足 30 m² 的对象仍保留。

**规则分块接缝可能漏过旧标记。** 已核对用户截图的组件 66791/69181：原网格中存在分界面，
模型高度为 6/3 m，两个组件此前均未被标记。疑似规则切块可形成各自有效的独立体，
旧的非流形/重叠检查无法覆盖。详见 [分缝来源核查](building-seams.md)，未自动合并或改高。

**道路折线还不能当成可路由路网。** 道路图层有 574 条闭合线，可能混合中心线、道路边界、
交通岛等 CAD 表达；全部场地折线均在 Z=0，无法单靠高程判断桥隧分离。
应先识别道路表达与地理定位，再开展中心线提取、交叉口核验和图构建。
本次没有将这些折线自动 planarize 后导入已有城市图。

## 已输出和验证

- `building_candidates.parquet`：111,347 个带局部工程 CRS 的候选底轮廓与模型高度。
- `building_candidate_metrics.csv`：候选编号、网格来源、模型标高、高度、momepy 形态指标和质量标记。
- `context_linework.parquet`：原始道路、水系、绿地、铁路折线，保留三维坐标与来源 UUID。
- `suzhou_model_local.gpkg`：可在 QGIS 中打开，包含建筑候选和四类场地线，共 5 个图层。
- `report.json`、`object_catalogue.json`、`footprint_overlap_reviews.json`：来源、提取和复核记录。
- `geopackage-verification.json`：真实写入后读回，候选数、编号、面积和局部 CRS 一致。
- `preview/footprints_heights.png`：全域及局部窗口的静态核验图；已经实际查看。

候选 ID 唯一、底轮廓有效、面积和高度为正；形态指标没有非有限值。
这些检查不代表已验证建筑栋数、测绘精度、实际覆盖范围和道路可达性。

测试包括未焊接立方体、抬高底标高、XY 重叠但空间分离、角点接触、完整面接触、
凹形轮廓、重叠标记和真实 `.3dm → GeoParquet → GeoPackage` 往返。

## 复现

```bash
python -m pip install -e ".[rhino,plot]"
.venv/bin/python scripts/inspect_rhino_model.py \
  'source_files/苏州城市模型.3dm' \
  --output data/suzhou_rhino_model --geopackage
.venv/bin/python scripts/render_rhino_extract.py data/suzhou_rhino_model
```

官方库：[McNeel rhino3dm](https://github.com/mcneel/rhino3dm)、
[Python API 文档](https://mcneel.github.io/rhino3dm/python/api/)。

现阶段适合进行局部坐标中的建筑形态分类和模型高度描述。
使用绝对高度进行日照、容积率或建筑容量判断前，需要确认高度及模型比例的来源和误差。

## 当前选定的古城区分析窗口

按用户最新指定的模型局部坐标，范围为 **X=23–29 km、Y=0–5 km**，即
`[23000, 0, 29000, 5000]` 米，矩形面积 30 km²。这里记录选定的分析窗口，
不将它当作已核验的古城区保护边界。此前的试选窗口不再作为研究范围。

独立配置为 `configs/suzhou-old-city.yaml`，结果保存在 `data/suzhou_old_city/`：

- 与窗口有正面积交集的建筑候选 22,700 个，其中质心位于窗口内 22,570 个。
- 289 个候选跨越窗口边界；保留完整轮廓计算形状，同时另存窗口内面积。
- 道路、水系、绿地、铁路来源线分别为 3,730、71、474、98 条；同时保留完整来源线和裁切版本。
- 模型高度中位数 6 m，95 分位 18 m，范围 3–96 m。
- 全部底轮廓并集在窗口内约 8.4238 km²，模型轮廓覆盖率约 28.08%，不代表已验证的现状覆盖率。
- 原始几何质量标记继续保留，其中 3,005 个候选轮廓待核。

更新图：`data/suzhou_old_city/preview/footprints_heights.png`。
GIS 数据：`data/suzhou_old_city/suzhou_old_city_local.gpkg`，包含窗口边界、建筑候选和完整/裁切场地线。
配置、预览、GeoParquet 与 GeoPackage 的坐标边界已核对一致。

```bash
.venv/bin/python scripts/select_rhino_area.py --config configs/suzhou-old-city.yaml
.venv/bin/python scripts/render_rhino_extract.py data/suzhou_rhino_model \
  --window-km 23 0 29 5 --region-name 古城区分析窗口 \
  --output-dir data/suzhou_old_city/preview
```
