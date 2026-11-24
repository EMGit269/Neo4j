# 高德 POI：古城区预实验获取方式

官方文档核对日期：2026-11-24。当前没有配置 `AMAP_WEB_KEY`，没有发送真实 POI 请求。
接口程序已完成离线测试与 dry-run；实时权限、配额和返回字段要用实际账户验证。

## 申请与接口

在 [高德开放平台](https://console.amap.com/) 注册，按
[创建项目和 Key](https://lbs.amap.com/api/webservice/create-project-and-key) 建应用、添加 Key，
服务平台选择 **Web 服务**。本项目调用 HTTP 服务，不使用 JS API 类型 Key。

Key 配置在本机私有 `.env`：`AMAP_WEB_KEY=实际值`。示例变量见 `.env.example`。
日配额、QPS、可用功能与费用以当前账户控制台为准；
[流量限制说明](https://lbs.amap.com/api/webservice/guide/tools/flowlevel) 指向控制台配额管理与产品定价。

本研究范围适合用 [地点搜索 2.0](https://lbs.amap.com/api/webservice/guide/api-advanced/newpoisearch)
的多边形接口：

```text
GET https://restapi.amap.com/v5/place/polygon
```

主要参数为 `key`、`polygon`（GCJ-02 的经度,纬度）、明确的 `types` 分类码、
`page_size`（1–25）、`page_num`。多个分类码用 `|` 连接。
一般多边形首尾闭合；矩形也可用左上、右下两个点。

不要省略 `types`：官方文档写明缺省仅商务住宅和交通设施两类，并非城市全部 POI。
常见研究分类可从 [官方分类码表](https://lbs.amap.com/api/webservice/download) 选取：

| 大类码 | 内容 | 可支持的初步研究变量 |
|---|---|---|
| 050000 | 餐饮服务 | 餐饮点位分布 |
| 060000 | 购物服务 | 商业点位分布 |
| 070000 | 生活服务 | 日常服务点位 |
| 080000 | 体育休闲服务 | 休闲设施点位 |
| 090000 | 医疗保健服务 | 医疗设施分布 |
| 110000 | 风景名胜 | 景点和部分文化场所 |
| 140000 | 科教文化服务 | 教育、文化等点位，需继续筛子类 |
| 150000 | 交通设施服务 | 交通站点等，需按研究目的筛子类 |

大类可能混入培训、经营机构等对象；“科教文化服务”不能直接等同于“公共教育资源”。
做指标前需确定子类、父子 POI 合并、缺失值和重复点处理口径。

## 可以取得什么，不能据此证明什么

基础字段包括 ID、名称、分类/typecode、位置、地址、行政区等。
可请求 `show_fields=business,navi,children` 获取可用的业务、导航和子 POI 信息；
入口 `navi.entr_location` 等字段可能缺失，不是每个 POI 都有真实门口位置。
点位本身不证明经营面积、实际开放性、设施容量、建筑内全部功能或合法步行入口。

官方明确说明：**不支持返回全量数据，同一组参数翻页最多 200 条**。
v5 的 `count` 是本次请求实际返回的条数，不是整个区域总数。
因此不能把一次古城区查询得到的点数当作真实 POI 总量，不能据此宣称数据完整或跨街区可比。
采样程序遇到页上限、200 条或请求预算会停止并记录截断可能性，不自动拆网格绕过限制。
如后续需要完整覆盖，应先核对账户可获得的数据产品/授权，或采用明确、可审计的抽样设计。

## 坐标必须显式处理

1. 当前恢复出的模型地图与 OSM 使用 WGS84。
2. 高德在中国大陆的 POI 与查询区域使用 GCJ-02；它不能被标成 EPSG:4326。
3. 程序先调用[官方坐标转换](https://lbs.amap.com/api/webservice/guide/api/convert)，
   `coordsys=gps` 将 WGS84 查询区域转成高德坐标，单次最多 40 点。
4. 返回 POI 原样保存为 `longitude_gcj02`、`latitude_gcj02`，以及原始 JSON。
   当前没有输出伪装为 WGS84 的 POI GeoJSON，也没有直接做建筑空间匹配。

pyproj 的 EPSG 转换不会完成 WGS84↔GCJ-02。上述官方接口没有在该文档中提供 GCJ-02→WGS84 逆转换。
后续如采用本地近似逆算法，需要标记估算方法、保存原坐标并核查偏差；
或者把对照几何通过明确的官方正向转换放入同一坐标空间再比较。

## 现成的 500 m 预实验

默认在已恢复的古城区窗口中心选取约 500×500 m 小范围，查询科教文化类，最多 2 页、约 50 条。
总请求预算默认 4 次，包含区域坐标转换；默认间隔至少 1 秒，仍需按账户 QPS 调整。

先查看计划，不需要 Key，也不会访问高德：

```bash
.venv/bin/python scripts/fetch_amap_pois.py
```

本机 `.env` 配置有效 Web 服务 Key 后运行：

```bash
.venv/bin/python scripts/fetch_amap_pois.py --execute \
  --types 140000 --pilot-size-m 500 --max-pages 2 --max-requests 4
```

产物在 `data/suzhou_old_city/amap_pilot/`：查询计划、原始返回、GCJ-02 CSV、按 POI ID 去重的 JSON、
停止原因与采集清单。失败记录同样保存；请求日志不保存 Key。
测试验证的是 API 协议处理、分页语义、去重、请求预算与密钥脱敏，尚未验证真实接口可用性。

先用这批样本核对分类、位置与字段质量，再决定是否扩展为服务可达性、功能混合或街区更新研究。
现有 SHP 也含 OSM 点/面 POI，可先用于地标和覆盖差异核查，不能直接视为高德数据的等价替代。
