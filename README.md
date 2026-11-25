# Urban Fabric Lab

面向城市设计研究的 Rhino / GeoPandas / momepy / OSMnx / Neo4j 代码库。
当前研究数据为苏州城市模型，保留窗口 **模型 X=23000–29000、Y=0–5000**。
已恢复 WGS84 地理位置，窗口约 5.71×5.54 km，投影面积 **31.6386 km²**。

## 当前数据与入口

- 原始模型：`source_files/苏州城市模型.3dm`，只读保留。
- 全域提取：`data/suzhou_rhino_model/`，111,347 个建筑几何候选单元。
- 古城区分析窗口：原始 22,700 个相交候选，合并全部网格疑似分缝及内部多部件接缝后为 **21,827 个轮廓**。
- 当前建筑：`data/suzhou_old_city/repaired/`，944 个候选含接缝合并，待处理疑似分缝为 0。
- 窗口配置：[suzhou-old-city.yaml](configs/suzhou-old-city.yaml)。
- 详细方法和数据限制：[苏州模型说明](docs/suzhou-rhino-model.md)。
- 配准方法、转换公式：[坐标恢复说明](docs/suzhou-georeferencing.md)。
- 当前建筑 GIS：`data/suzhou_old_city/repaired/suzhou_old_city_repaired.gpkg`。
- 当前建筑指标：`data/suzhou_old_city/repaired/building_metrics.csv`。
- 参考道路/水面及原建筑 GIS：`data/suzhou_old_city/georeferenced/suzhou_old_city_projected.gpkg`。
- OSM POI：`data/suzhou_old_city/osm_pois/`，4,707 个候选筛选并去重后保留 1,785 条，160 条待核。
- POI 地图：`http://127.0.0.1:8766/suzhou_old_city/osm_pois/map.html`，支持类别和名称筛选。
- 叠合地图：`http://127.0.0.1:8766/suzhou_old_city/georeferenced/map.html`。
- 最新图：`data/suzhou_old_city/georeferenced/georeferencing_preview.png`。
- 本机预览入口：`http://127.0.0.1:8766/`（需本地预览服务运行）。

建筑保持完整轮廓，另存窗口内面积和边界标记。道路、水系、绿地、铁路同时提供完整来源线与裁切版本。
建筑模型高度为 3–96 m（当前窗口），全部为 3 m 的整数倍，来源和制作方法尚未核实。
模型 XY 实为经纬度×100000 后平移，Rhino 的“米”声明不能当成真实平面比例；
已通过 66,981 条对应道路线段恢复坐标，并在真实投影下重算指标。
已合并全模型检测到的重复网格分缝，包括单个多部件轮廓内部的分缝；
合并组的 `height_extent_m` 采用源块最高模型高度，同时保留各源块高度和冲突范围。
这一高度未经测绘核验，棱柱体积是按最高源块高度计算的估算上界。
方法、来源对应和验证见 [建筑分缝修复](docs/building-seams.md)。
旧局部数据保留用于溯源。**建筑单元和模型高度尚未全部核验，道路尚未确认为可路由网络**。

## 安装和复现

建议 Python 3.12，无需显卡。使用独立虚拟环境：

```bash
git clone https://github.com/EMGit269/Neo4j.git
cd Neo4j
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,rhino,pbf,plot]"
# Windows PowerShell 使用 .venv\Scripts\Activate.ps1 激活
```

原始 `.3dm`、配套 SHP 和当前 GIS/地图通过
[Release 数据包](https://github.com/EMGit269/Neo4j/releases/tag/suzhou-data-20261125) 提供。
Git 克隆不包含这些大文件；下载脚本按清单验证大小和 SHA-256，并保留已有研究文件。
完整恢复请使用干净的克隆或空目标目录，详见 [数据清单与恢复说明](datasets/README.md)。

```bash
python scripts/download_research_data.py
# 只下载当前分析数据和可浏览地图：
python scripts/download_research_data.py --part analysis
```

下载分析包后可直接启动下方预览服务，无需重新提取。没有下载数据时，可先离线验证通用流程：

```bash
urban-fabric demo --output data/demo
urban-fabric import-neo4j --graph-dir data/demo/graph --dry-run
```

需要从源数据重新计算时，按以下顺序执行。分析包已有全域提取，可跳过第一步：

```bash
python scripts/inspect_rhino_model.py \
  'source_files/苏州城市模型.3dm' \
  --output data/suzhou_rhino_model --geopackage
```

裁取窗口并绘图：

```bash
.venv/bin/python scripts/select_rhino_area.py --config configs/suzhou-old-city.yaml
.venv/bin/python scripts/render_rhino_extract.py data/suzhou_rhino_model \
  --window-km 23 0 29 5 --region-name 古城区分析窗口 \
  --output-dir data/suzhou_old_city/preview
```

原坐标裁取后，恢复地理位置并重算物理指标：

```bash
.venv/bin/python scripts/georeference_suzhou.py --shp-dir 'source_files/苏州shp新'
.venv/bin/python scripts/render_georeferenced_suzhou.py
```

分缝修复与当前地图：

```bash
.venv/bin/python scripts/repair_building_seams.py
.venv/bin/python scripts/render_seam_repair.py
.venv/bin/python scripts/render_georeferenced_suzhou.py
```

高德 POI 的 [申请、坐标与采样说明](docs/amap-poi.md) 已准备。
`.venv/bin/python scripts/fetch_amap_pois.py` 默认只生成查询计划；本地配置 Web 服务 Key 后加 `--execute`。

已获取的 OSM POI 使用 2026-11-23 江苏区域快照，剔除树、路灯、消防设施等对象，保留原始数据和审计清单。
规则、实际分类统计与覆盖限制见 [OSM POI 说明](docs/osm-pois.md)。

```bash
# 分析包已包含采集原始数据，可直接筛选；缺少原始数据时先执行采集。
# 新的在线采集会使用当时的 OSM 数据，结果可能与快照不同。
.venv/bin/python scripts/acquire_osm_pois.py
.venv/bin/python scripts/filter_osm_pois.py
.venv/bin/python scripts/render_osm_pois.py
```

启动本地文件预览（只监听本机）：

```bash
.venv/bin/python -m http.server 8766 --bind 127.0.0.1 --directory data
```

## 指标与后续工作

已计算建筑候选面积、周长、紧凑度、伸长比、矩形度和模型竖向高度。
详见 [指标口径](docs/metrics.md)、[清洗规则](docs/data-cleaning.md)、[预研究安排](docs/prestudy-plan.md)。
GeoPandas / Shapely 处理几何，momepy 计算形态，NetworkX 运行网络算法，Neo4j 管理对象关系与来源。
OSM / GBA 获取、语义保护清洗和图导出能力仍保留为通用工具。
苏州模型已有真实投影和 WGS84 文件，尚未完成道路拓扑验收及图导出。

## Neo4j

本机已有 Neo4j 服务；新环境可使用 Docker Compose，先复制 `.env.example` 为 `.env` 并设置密码，
再运行 `docker compose up -d`。`.env` 不随代码或数据包分发。
旧南京图已在备份后按研究区 ID 清理，苏州数据尚未导入：
已配准的 GIS 可作为下一阶段输入，道路线仍需要先确认拓扑。
当前图模式和导入边界见 [图模式](docs/graph-schema.md)。

以下管理脚本仅适用于通过 `scripts/install_neo4j_local.py` 安装的本地服务：

```bash
.venv/bin/python scripts/neo4j_local.py status
.venv/bin/python scripts/neo4j_local.py start
.venv/bin/python scripts/neo4j_local.py stop
```

Neo4j Browser：`http://127.0.0.1:7474/browser/`；认证信息保存在私有 `.env`，不公开。
基础导入器不依赖 APOC 或 GDS。独立合成示例可验证通用 GIS→图文件流程：

```bash
.venv/bin/urban-fabric demo --output data/demo
.venv/bin/urban-fabric import-neo4j --graph-dir data/demo/graph --dry-run
```

## 开发与历史整理

```bash
.venv/bin/pytest
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts
```

通用代码、测试、运行环境和论文资料保留。旧南京数据、下载缓存和旧调试产物已移至 macOS 废纸篓；
文档、配置及 Neo4j 可恢复图备份见 [历史整理记录](docs/history-cleanup.md)。
`data/` 与 `source_files/` 通过 Release 分发；`outputs/`、`.local/`、凭据和虚拟环境不进入代码版本或数据包。
