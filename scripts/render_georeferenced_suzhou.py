"""Render a metric research map and a WGS84 Leaflet overlay for calibration review."""

import argparse
import json
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.lines import Line2D


def render(root):
    font = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
    if font.exists():
        font_manager.fontManager.addfont(str(font))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(font)).get_name()
    plt.rcParams["axes.unicode_minus"] = False
    report = json.loads((root / "georeferencing.json").read_text())
    repaired = root.parent / "repaired"
    use_repaired = (repaired / "repair_report.json").exists()
    buildings = gpd.read_parquet((repaired if use_repaired else root) / "buildings.parquet")
    area = gpd.read_parquet(root / "analysis_window.parquet")
    roads = gpd.read_parquet(root / "reference_osm_roads.parquet")
    water = gpd.read_parquet(root / "reference_osm_water.parquet")
    model_lines = gpd.read_parquet(root / "context_linework.parquet")
    pois = gpd.read_parquet(root / "reference_osm_pois.parquet")
    fig, axes = plt.subplots(1, 2, figsize=(17, 9), gridspec_kw={"width_ratios": [1.25, 1]})
    fig.set_facecolor("#f5f4ef")
    for ax in axes:
        ax.set_facecolor("#f5f4ef")
        water.plot(ax=ax, facecolor="#c3dce1", edgecolor="none")
        roads.plot(ax=ax, color="#81908d", linewidth=0.35)
        ax.tick_params(labelsize=8)
        ax.ticklabel_format(useOffset=False, style="plain")
        ax.set_xlabel("东坐标 / m · SHP 自定义横轴墨卡托投影")
        ax.set_ylabel("北坐标 / m")
        ax.set_aspect("equal")
    buildings.plot(
        ax=axes[0],
        column="height_extent_m",
        cmap="viridis",
        vmin=3,
        vmax=60,
        linewidth=0,
        rasterized=True,
        missing_kwds={"color": "#bd916f"},
    )
    area.boundary.plot(ax=axes[0], color="#b5643e", linewidth=1.5)
    west, south, east, north = area.total_bounds
    axes[0].set_xlim(west - 130, east + 130)
    axes[0].set_ylim(south - 130, north + 130)
    axes[0].set_title("地理定位后的完整分析窗口", loc="left", pad=12)
    # A named garden makes the detailed location independently recognisable.
    garden = pois[pois["name"].fillna("").eq("拙政园")]
    if len(garden):
        center = garden.geometry.iloc[0]
    else:
        center = area.centroid.iloc[0]
    model_roads = model_lines[model_lines.role == "road_linework"]
    model_roads.plot(ax=axes[1], color="#cb693b", linewidth=0.75, linestyle="--")
    axes[1].set_xlim(center.x - 750, center.x + 750)
    axes[1].set_ylim(center.y - 650, center.y + 650)
    axes[1].scatter([center.x], [center.y], color="#254c49", s=25, zorder=5)
    axes[1].annotate(
        "拙政园（OSM 标注点）" if len(garden) else "分析窗口中心",
        (center.x, center.y),
        xytext=(7, 8),
        textcoords="offset points",
        fontsize=10,
    )
    axes[1].set_title("道路叠合核对 · 实线 OSM / 橙色虚线模型", loc="left", pad=12)
    axes[1].legend(
        handles=[
            Line2D([0], [0], color="#81908d", label="下载的 OSM 道路"),
            Line2D([0], [0], color="#cb693b", linestyle="--", label="恢复地理位置的模型道路"),
        ],
        loc="lower right",
        fontsize=9,
    )
    fig.suptitle("苏州古城区：模型坐标恢复与地图叠合", x=0.06, ha="left", fontsize=22)
    fig.text(
        0.06,
        0.91,
        f"原模型窗口 X=23–29、Y=0–5（千模型单位） · 实际投影面积 "
        f"{report['window_area_projected_km2']:.2f} km² · {len(buildings):,} 个建筑候选",
        fontsize=11,
        color="#42645c",
    )
    fig.text(
        0.06,
        0.05,
        "66,981 条同源道路对应线段支持坐标恢复；不代表测绘精度。"
        "模型高度未经实测核验，棕色为源块高度冲突；建筑与参考 SHP 的轮廓并非逐栋一致。",
        fontsize=10,
        color="#52685f",
    )
    fig.subplots_adjust(left=0.06, right=0.98, top=0.86, bottom=0.15, wspace=0.22)
    fig.savefig(root / "georeferencing_preview.png", dpi=160, facecolor=fig.get_facecolor())
    plt.close(fig)

    html = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>苏州地图叠合核对</title>
<link rel="stylesheet" href="../../assets/leaflet-1.9.4/leaflet.css">
<style>body{margin:0;background:#f6f5ef;color:#25463e;font:15px/1.6 system-ui}
header{padding:16px 24px}h1{font-size:23px;margin:0}p{margin:5px 0}
#map{height:76vh;min-height:500px}aside{padding:12px 24px}a{color:#327269}img{width:100%}
.leaflet-control-layers{line-height:1.8}</style>
<header><h1>苏州古城区 · 地理定位后的模型</h1>
<p>原模型窗口 X=23–29、Y=0–5（千模型单位） · 真实投影面积 AREA_KM2 km² ·
BUILDING_COUNT 个建筑候选</p>
<p id="status">正在加载地图；下方静态核验图无需在线底图。</p></header><div id="map"></div>
<aside><p>坐标：WGS84。道路叠合验证坐标恢复；模型高度和建筑实体仍需核实。
<a href="suzhou_old_city_projected.gpkg">下载投影 GIS</a> ·
<a href="../repaired/suzhou_old_city_repaired.gpkg">下载合并后建筑 GIS</a> ·
<a href="georeferencing.json">配准报告</a> ·
<a href="../osm_pois/map.html">筛选后的 OSM POI</a> ·
<a href="../../index.html">返回数据入口</a></p>
<img src="georeferencing_preview.png" alt="投影与道路叠合核验图"></aside>
<script src="../../assets/leaflet-1.9.4/leaflet.js"></script><script>
const statusEl=document.getElementById('status');
if(typeof L==='undefined'){statusEl.textContent='在线地图库未载入，请查看下方静态图或下载 GIS。';}
else{const map=L.map('map',{preferCanvas:true});
const base=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
 {maxZoom:19,attribution:'© OpenStreetMap contributors'}).addTo(map);
const control=L.control.layers({'OSM 底图（WGS84）':base},{}).addTo(map);
L.control.scale({imperial:false}).addTo(map);
const renderer=L.canvas({padding:.3});
const activeBuildings=ACTIVE_BUILDINGS;
const descriptions=[
 ['analysis_window','分析窗口',{color:'#b35f38',weight:2,fill:false},true],
 ['reference_osm_water','OSM 水面',{color:'#86b4c4',weight:1,fillOpacity:.35},true],
 ['reference_osm_roads','OSM 道路',{color:'#687775',weight:1.4},true],
 ['context_linework','模型场地线',{color:'#dd7543',weight:1,dashArray:'4 3'},false],
 [activeBuildings,'当前建筑（棕色表示高度冲突）',
  {weight:.3,color:'#4e7971',fillOpacity:.65},true],
 ['reference_osm_pois','参考 SHP POI（旧文件）',{color:'#9f3d4b'},false],
 ['../osm_pois/poi_map','本次筛选后的 OSM POI',{color:'#a44068'},false]
];
if(activeBuildings!=='buildings')descriptions.push(
 ['buildings','合并前建筑轮廓（对照）',
  {weight:1,color:'#3e7bae',dashArray:'4 3',fill:false},false]);
Promise.all(descriptions.map(async([file,label,style,visible])=>{
 const response=await fetch(file+'_wgs84.geojson');if(!response.ok)throw Error(file);
 const data=await response.json();const layer=L.geoJSON(data,{renderer,
 style:f=>{if(file===activeBuildings){const h=f.properties.height_extent_m||0;
 return {...style,fillColor:f.properties.height_conflict?'#bd916f':
 h>30?'#876a28':h>12?'#438d77':'#79b3a1'};}return style;},
 pointToLayer:(f,ll)=>L.circleMarker(ll,{radius:4,...style,renderer}),
 onEachFeature:(f,l)=>{const p=f.properties,el=document.createElement('div');
 el.textContent=[p.name,p.id||p.osm_id||p.osm_uid,p.category,p.role||p.poi_role,
 p.height_conflict?'整栋取源块最高模型高度 '+p.height_extent_m+' m（源块范围 '+
  p.model_height_min_m+'–'+p.model_height_max_m+' m）':
 p.height_extent_m?'模型高 '+p.height_extent_m+' m':'',
 p.seam_merged?'已合并 '+p.source_component_count+' 个源块':'']
 .filter(Boolean).join(' · ');l.bindPopup(el);}});
 control.addOverlay(layer,label);if(visible)layer.addTo(map);
 if(file==='analysis_window')map.fitBounds(layer.getBounds());
})).then(()=>statusEl.textContent='图层已加载。可开关模型场地线，与 OSM 道路叠合核对。')
 .catch(e=>statusEl.textContent='部分图层未加载，请查看下方静态图。');}
</script></html>"""
    (root / "map.html").write_text(
        html.replace("AREA_KM2", f"{report['window_area_projected_km2']:.2f}")
        .replace("BUILDING_COUNT", f"{len(buildings):,}")
        .replace(
            "ACTIVE_BUILDINGS", json.dumps("../repaired/buildings" if use_repaired else "buildings")
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/suzhou_old_city/georeferenced"))
    render(parser.parse_args().data_dir)
