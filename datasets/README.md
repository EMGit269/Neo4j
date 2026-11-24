# 苏州模型与研究数据

数据通过本仓库的 [Release](https://github.com/EMGit269/Neo4j/releases/tag/suzhou-data-20261124) 分发。
原始 `.3dm` 为 115,309,193 字节，超过 GitHub 普通 Git 文件 100 MiB 的限制，
因此模型和批量 GIS 作为 Release 附件保存，代码库记录下载地址及 SHA-256。

| 数据包 | 内容 | 解压路径 |
|---|---|---|
| `suzhou-sources-20261124.zip` | 原始 Rhino 模型、配准使用的建筑 SHP、OSM SHP 完整文件族 | `source_files/` |
| `suzhou-analysis-20261124.zip` | 全域提取的必要输入、古城区当前/修复前 GIS、POI、来源审计和本地地图 | `data/` |

全域重复的 GeoPackage、全域指标 CSV、探索性匹配缓存和浏览器临时截图不重复打包，
可由原模型与代码重新生成。当前分析窗口的可直接使用文件、CSV 和地图均保留。
原始源文件不修改；派生文本报告中的本机绝对路径改为相对于仓库根目录的路径。

安装依赖后，在仓库根目录下载并校验：

```bash
python scripts/download_research_data.py
# 只需当前 GIS 和地图时：
python scripts/download_research_data.py --part analysis
# 如当前目录已有研究结果，会保留已有文件；完整恢复请指定空目录。
python scripts/download_research_data.py --destination /path/to/empty-directory
```

也可从 Release 页面手动下载两个 ZIP，校验清单见
[`suzhou-20261124.json`](suzhou-20261124.json)，然后解压到仓库根目录。
ZIP 内的根目录已经是 `source_files/` 或 `data/`。

当前模型研究窗口：X=23000–29000、Y=0–5000 模型单位，真实投影面积约 31.6386 km²。
分缝修复后为 22,073 个建筑候选轮廓，623 个合并组，480 个候选仍有疑似接缝待核。
618 个合并组的源块高度冲突，保留源高度与范围，未指定统一整栋高度。
OSM POI 保留 1,785 条，数据快照为 2026-11-23 00:34:06 UTC。

OSM 数据署名：© OpenStreetMap contributors，ODbL-1.0；
区域 PBF 来源为 [OpenStreetMap France](https://download.openstreetmap.fr/extracts/asia/china/)。
原 Rhino 与自定义建筑 SHP 的来源年份、模型高度与真实建筑实体仍按研究文档标记为待核。
