# 古城区 OSM POI 获取与筛选

本次任务范围：已配准的 X=23000–29000、Y=0–5000 模型选框，
WGS84 约 `[120.5881024174,31.2848285659,120.6481024174,31.3348285659]`。
不是苏州市行政全域，也不是古城保护边界。窗口约 31.64 km²。

## 本次实际结果（2026-11-24）

区域 PBF 快照为 **2026-11-23 00:34:06 UTC**，90,044,329 字节，通过发布方 MD5 验证。
提取并组装得到 4,707 个候选：2,092 个 node、2,570 个 way、45 个 relation。

| 处理结果 | 数量 |
|---|---:|
| 保留并保守去重后的 POI 记录 | 1,785 |
| 剔除 | 1,344 |
| 自然/土地覆盖等背景 | 1,414 |
| 待核 | 160 |
| 合并为重复记录 | 4 |
| 合计 | 4,707 |

剔除含 657 个树/树列对象、43 个路灯、5 个消防设施。
4 条重复记录为狮子林、网师园、三元坊、乐桥的点/面重复表达，原始 ID 均保留。
160 条待核中 159 条仅有建筑类型，1 条旅游信息设施类型不明；没有据名称猜测用途。
另有 **33 个集合关系**单列审计：32 个交通 stop_area/group 和 1 个“苏州古典园林”遗产集合，
不强行定位为一个新 POI，也没有丢弃其成员关系与标签。

| 分类 | 保留记录 |
|---|---:|
| 交通服务 | 768 |
| 休闲体育 | 247 |
| 餐饮 | 204 |
| 零售商业 | 142 |
| 文化与旅游 | 105 |
| 教育科研 | 100 |
| 生活服务 | 62 |
| 住宿 | 44 |
| 金融服务 | 31 |
| 公共与社会服务 | 22 |
| 宗教与殡葬 | 18 |
| 医疗健康 | 17 |
| 办公与生产 | 11 |
| 自然景观 | 11 |
| 市政设施 | 2 |
| 社会组织 | 1 |

交通类 768 条中有公交停靠点 370、停车位置 108、车站出入口 56、车站 37、站台 16，
另含停车场、共享单车和其他交通服务，**不能称为 768 座车站**。
630 条记录没有名称，仍保留其明确的功能标签；34 个场所源几何跨越研究区边界。
所有显示用点位都位于窗口内、ID 唯一；GeoPackage 逐层写入读回核验通过。

## “所有 POI”的数据口径

OSM 没有一张统一完备的 POI 总表。使用明确的标签并集查询场所、功能与背景候选，
再筛选对象；没有分页、排名或抽样数量上限，但不能据此声称包含现实中的全部店铺和设施。

原始候选纳入 `amenity/shop/tourism/leisure/historic/office/craft/healthcare/club/government` 等，
并加入交通站点、功能建筑类型、自然/市政/电力/消防等候选用于明确剔除与审计。
完整标签、取值及筛选规则见 `configs/osm-poi-filter.yaml`。
OSMnx 的多标签查询是并集，结果包括节点、线/面 way 与支持的 relation 面对象。
不把所有普通建筑、路段、行政区等对象统称 POI，也不从建筑外形猜测功能。

在线 Overpass 在本次获取时出现超时/拥堵，因此采用 OpenStreetMap France 的江苏区域 PBF，
在本地按窗口提取，PyOsmium 保留完整成员引用，OSMnx 组装实际几何。
区域快照、下载校验值、提取来源与软件版本记录在 `source/acquisition.json`。
当前 PBF 提取依据为落在窗口内的节点、与窗口相交的 way，以及具有这些成员的匹配 relation。
它不承诺恢复成员几何完全在窗口外但包围窗口的大范围 relation，
OSMnx 不支持的 relation 类型也会进入未组装 ID 记录，不假装已输出全部 relation 几何。

## 筛选规则

| 处理 | 内容 |
|---|---|
| 保留 | 餐饮、零售、教育、医疗、文化旅游、住宿、休闲体育、公共社会服务、金融、生活服务、交通、办公生产等明确功能 |
| 剔除 | 树、树列、路灯、消防设施（包括消防站）、消防器材，以及座椅、垃圾桶、邮筒、旅游信息牌等设备或符号 |
| 背景 | 普通土地覆盖、未命名自然面、住宅庭院、交通分组 relation 等，不计为单个服务场所 |
| 待核 | 只有 school/hospital 等建筑类型、功能未分类或标签不够明确的对象，单列而不猜测 |

未命名但有明确用途的对象保留。标签 `access=private` 也保留为原始属性，
因为本次是城市功能盘点，不把“被绘制”解释为“向公众开放”。
`disused/abandoned/construction` 等停用或规划状态不作为现状服务点，
有明确历史遗存属性的对象可作为文化遗产保留。
名称不自动翻译或补造，原始全部标签保存在 `tags_json`。

消防站按用户本次“消防设施不需要”的要求一起排除；如后续研究公共安全服务，可在规则中恢复。
公厕、ATM、租车/共享单车点和充电点保留，但通过 `poi_role=service_point` 与一般场所区分。
地铁站、站台、停车位置、公交停靠点、出入口也保留各自 `poi_role`；
不能把所有这些记录简单相加后称为“交通站数量”。

## 空间处理与去重

1. OSM 使用 WGS84，转换到当前 SHP 的完整自定义米制投影。与 GCJ-02 高德数据分开。
2. 对原几何与分析窗口求相交；面和线保留完整源几何，并标明跨界。
3. 对面和线在窗口内的部分生成内部代表点，保证点在研究区内，避免凹形面质心落到面外。
   该点仅用于地图和空间分析，不能解释为主门、步行入口或实际经营位置。
4. 使用 OSM `类型+ID` 作为唯一身份，避免 node/way/relation 数字 ID 冲突。
5. 同名、同细分类且点在面内，或同类别且 Wikidata 一致时，才视为点/面重复候选。
   同一实体的面/面还要求交并比至少 0.98；优先保留面和 relation，记录所有来源 ID。
6. 多个面均可能对应某点时，保留并标记歧义；不同细分类、异地同名分店不按距离合并。

这是保守去重，仍可能存在名称变体和校园/园区分层重复，不宣称每条记录都是独立法人或独立建筑。
OSMnx 可能在组装时修复几何；原始 `.osm` 保留，可以追溯其前后差异。

## 文件与复现

所有结果在 `data/suzhou_old_city/osm_pois/`。

| 文件 | 用途 |
|---|---|
| `source/osm_poi_window.osm` | 完整引用的原始 OSM 提取 |
| `source/osm_features_wgs84.parquet` | 原始候选几何与原标签 |
| `source/acquisition.json` | 来源、快照时间、校验值、版本与获取边界 |
| `source/unassembled_selected_ids.json` | 选到但未由 OSMnx 输出的对象身份，需结合 relation 语义理解 |
| `source/unassembled_relations.csv/json` | 上述集合关系的名称、全部标签与成员 ID，保留而不假造点位 |
| `all_candidates.parquet/csv` | 每个候选的决定、分类与理由，数量可闭合 |
| `poi_features.parquet` | 保留且去重后的完整场所几何 |
| `poi_points.parquet/csv` | 场所点、类别、名称、WGS84 经纬度和全部标签 |
| `poi_points_wgs84.geojson` | 完整点属性，WGS84 网络地图格式 |
| `poi_members.parquet/csv` | 去重前保留对象与合并来源 |
| `excluded/context/review/duplicates` | 剔除、背景、待核与重复清单，均提供 Parquet/CSV |
| `suzhou_old_city_osm_pois.gpkg` | 点、原始场所几何、待核和剔除层 |
| `category_counts.csv` / `summary.json` | 分类数量、质量记录和口径 |
| `map.html` / `poi_preview.png` | 可按类别/名称查看的地图与静态研究图 |

交互地图使用 `data/assets/leaflet-1.9.4/` 的本地 Leaflet 资源，许可见 `data/assets/LICENSE`。
在线 OSM 底图不可用时，仍可使用已有本地道路/水面图层查看 POI。
地图默认叠加 **22,073 个分缝修复后的 Rhino 建筑候选轮廓**，可在图层控件中开关；
POI 绘制在建筑上方，点击建筑可查看 ID、模型高度和轮廓复核标记。
623 个合并组保留全部源编号，冲突高度显示为范围；可打开原始 22,700 个轮廓作蓝色虚线对照。
地图还可标出已合并组与剩余待核轮廓。
这只是地图叠合展示，尚未建立 POI 与建筑之间的逐栋功能匹配。

```bash
# 在线方式，接口可用时使用；已有成功获取文件会按请求规格复用。
.venv/bin/python scripts/acquire_osm_pois.py

# 本次采用区域 PBF；需已安装 .[pbf] 可选依赖。
.venv/bin/python scripts/acquire_osm_pois.py \
  --pbf .local/osm-cache/suzhou-poi/jiangsu.osm.pbf
.venv/bin/python scripts/filter_osm_pois.py
.venv/bin/python scripts/render_osm_pois.py
```

筛选器从原始候选重跑，修改规则不会删除原始对象，也不需要重新下载。
若修改的是获取标签集或研究边界，需保留旧版本后在新路径重新获取。

来源署名：© OpenStreetMap contributors，ODbL-1.0。
官方依据：[OSMnx features](https://osmnx.readthedocs.io/en/stable/user-reference.html#osmnx.features.features_from_polygon)、
[OSM Map features](https://wiki.openstreetmap.org/wiki/Map_features)、
[区域数据提取](https://download.openstreetmap.fr/extracts/asia/china/)。
