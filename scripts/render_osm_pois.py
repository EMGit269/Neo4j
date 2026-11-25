"""Research preview and categorized web map of screened OSM POIs."""

import argparse
import json
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

from urban_fabric.pois import CATEGORY_LABELS

COLORS = {
    "food": "#c4743c",
    "retail": "#b65066",
    "education": "#3275a0",
    "health": "#b23e45",
    "culture_tourism": "#8961a0",
    "leisure_sport": "#529264",
    "lodging": "#a67a36",
    "public_services": "#475e9e",
    "finance": "#9e8840",
    "transport": "#637779",
    "life_services": "#9c6e8f",
    "religion": "#997465",
    "employment": "#537885",
    "municipal": "#777777",
    "landscape": "#3c9a8b",
    "organisation": "#785897",
}


def render(root):
    font = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
    if font.exists():
        font_manager.fontManager.addfont(str(font))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(font)).get_name()
    plt.rcParams["axes.unicode_minus"] = False
    summary = json.loads((root / "summary.json").read_text())
    points = gpd.read_parquet(root / "poi_points.parquet")
    georef = root.parent / "georeferenced"
    area = gpd.read_parquet(georef / "analysis_window.parquet").to_crs(points.crs)
    roads = gpd.read_parquet(georef / "reference_osm_roads.parquet").to_crs(points.crs)
    water = gpd.read_parquet(georef / "reference_osm_water.parquet").to_crs(points.crs)
    repaired = root.parent / "repaired"
    use_repaired = (repaired / "repair_report.json").exists()
    building_root = repaired if use_repaired else georef
    buildings = gpd.read_parquet(building_root / "buildings.parquet").to_crs(points.crs)
    fig, axes = plt.subplots(1, 2, figsize=(16, 9), gridspec_kw={"width_ratios": [1.3, 1]})
    fig.set_facecolor("#f6f5ef")
    for ax in axes:
        ax.set_facecolor("#f6f5ef")
    water.plot(ax=axes[0], facecolor="#c6dce1", edgecolor="none")
    roads.plot(ax=axes[0], color="#c0c7c1", linewidth=0.35)
    buildings.plot(ax=axes[0], facecolor="#b8c2ba", edgecolor="#87958b", linewidth=0.12)
    for code, subset in points.groupby("category_code"):
        subset.plot(
            ax=axes[0], color=COLORS.get(code, "#555555"), markersize=8, alpha=0.75, linewidth=0
        )
    area.boundary.plot(ax=axes[0], color="#555f52", linewidth=1)
    w, s, e, n = area.total_bounds
    axes[0].set_xlim(w - 80, e + 80)
    axes[0].set_ylim(s - 80, n + 80)
    axes[0].set_aspect("equal")
    axes[0].tick_params(labelsize=8)
    axes[0].ticklabel_format(useOffset=False, style="plain")
    axes[0].set_xlabel("东坐标 / m · SHP 自定义投影")
    axes[0].set_ylabel("北坐标 / m")
    axes[0].set_title("筛选并保守去重后的场所与服务点", loc="left", pad=12)
    categories = summary["categories"][::-1]
    codes = [row["category_code"] for row in categories]
    values = [row["poi_count"] for row in categories]
    labels = [row["category"] for row in categories]
    axes[1].barh(labels, values, color=[COLORS.get(code, "#555555") for code in codes], height=0.63)
    for y, value in enumerate(values):
        axes[1].text(value + max(values) * 0.018, y, str(value), va="center", fontsize=10)
    axes[1].set_xlim(0, max(values) * 1.17)
    axes[1].set_xlabel("保留的 OSM 对象 / 场所记录数")
    axes[1].set_title("分类统计 · 包含未命名的明确功能对象", loc="left", pad=12)
    axes[1].spines[["top", "right", "left"]].set_visible(False)
    axes[1].grid(axis="x", alpha=0.15)
    axes[1].set_axisbelow(True)
    counts = summary["counts"]
    fig.suptitle("苏州古城区 · OSM POI 筛选结果", x=0.065, ha="left", fontsize=23)
    fig.text(
        0.065,
        0.91,
        f"原始候选 {counts['source_features']:,} · 保留 {counts['kept_unique_pois']:,} · "
        f"剔除 {counts['excluded']:,} · 背景 {counts['context']:,} · "
        f"待核 {counts['review']:,} · 去重 {counts['duplicates']:,}",
        fontsize=11,
        color="#426057",
    )
    fig.text(
        0.065,
        0.055,
        "© OpenStreetMap contributors / ODbL · 场所覆盖不完整；点和面均纳入。"
        "面对象取内部代表点，不代表入口；站点、站台和出入口需分口径统计。",
        fontsize=10,
    )
    fig.subplots_adjust(left=0.065, right=0.97, top=0.85, bottom=0.14, wspace=0.35)
    fig.savefig(root / "poi_preview.png", dpi=170, facecolor=fig.get_facecolor())
    plt.close(fig)

    html = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>苏州 OSM POI</title>
<link rel="stylesheet" href="../../assets/leaflet-1.9.4/leaflet.css">
<style>body{margin:0;background:#f6f5ef;color:#29473f;font:15px/1.6 system-ui}
header{padding:16px 24px}h1{font-size:23px;margin:0}p{margin:5px 0}
#map{height:75vh;min-height:520px}
aside{padding:15px 24px}a{color:#316f62}img{width:100%}input{padding:5px;border:1px solid #b7c6bb}
.leaflet-control-layers{line-height:1.7;max-height:65vh;overflow:auto}.dot{display:inline-block;
width:9px;height:9px;border-radius:50%;margin-right:7px}button{padding:5px 12px;margin-left:8px}
</style><header><h1>苏州古城区 · OSM 场所与服务点</h1>
<p>COUNTS_TEXT</p><p id="status">正在载入。可按类别开关，点击点位查看原始 OSM 对象。</p>
<p id="building-status">建筑轮廓正在载入……</p>
<label>名称筛选 <input id="search" placeholder="输入名称；空白显示全部"></label>
<button id="all">全选类别</button><button id="none">清空类别</button></header>
<div id="map"></div><aside>
<p><a href="suzhou_old_city_osm_pois.gpkg">下载 GIS</a> · <a href="poi_points.csv">POI 表</a> ·
<a href="review.csv">待核清单</a> · <a href="excluded.csv">剔除清单</a> ·
<a href="summary.json">数据报告</a> · <a href="../../index.html">数据入口</a></p>
<p><a href="../repaired/suzhou_old_city_repaired.gpkg">下载修复后建筑 GIS</a> ·
<a href="../repaired/repair_report.json">建筑合并报告</a></p>
<p>灰色轮廓为已配准的 Rhino 模型建筑候选，可在右侧开关并点击查看模型高度。
建筑与 POI 共同展示，尚未建立逐栋功能对应。模型高度未经实测核验。</p>
<p>面、线场所已转换成窗口内代表点，不能视为实际入口。OSM 覆盖不完整，
交通站点、站台和出入口保留不同类型；数量统计应按研究目的进一步筛选。</p>
<img src="poi_preview.png" alt="OSM POI 分布与分类统计"></aside>
<script src="../../assets/leaflet-1.9.4/leaflet.js"></script><script>
const colors=COLORS_JSON,labels=LABELS_JSON,statusEl=document.getElementById('status');
const roleLabels={place:'场所',service_point:'服务点',station:'车站',platform:'站台',
stop:'停靠点',stop_position:'停车位置',station_entrance:'车站出入口',
sports_facility:'体育设施',landscape_site:'园林与休闲场地',heritage_site:'文化遗产',
visitor_service:'游客服务',industrial_site:'生产场所',natural_landmark:'自然景观',
public_space:'公共空间',cemetery:'墓地',recreation_site:'休闲场地',protected_site:'保护地'};
if(typeof L==='undefined'){statusEl.textContent='在线地图库未载入，可查看下方静态图和下载 GIS。';}
else{const map=L.map('map',{preferCanvas:true});
const tile=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
{maxZoom:19,attribution:'© OpenStreetMap contributors'}).addTo(map);
const control=L.control.layers({'OSM 底图':tile},{},{collapsed:false}).addTo(map);
L.control.scale({imperial:false}).addTo(map);const categories={};
map.createPane('context').style.zIndex=350;
map.createPane('buildings').style.zIndex=410;
map.createPane('originalBuildings').style.zIndex=420;
map.getPane('context').style.pointerEvents='none';
map.getPane('originalBuildings').style.pointerEvents='none';
const contextRenderer=L.canvas({padding:.3,pane:'context'});
const buildingRenderer=L.canvas({padding:.3,pane:'buildings'});
// Share one interactive canvas so an empty POI canvas cannot swallow building clicks.
const renderer=buildingRenderer;
fetch(BUILDING_URL).then(r=>{
if(!r.ok)throw Error('buildings');return r.json();}).then(data=>{
const layer=L.geoJSON(data,{renderer:buildingRenderer,pane:'buildings',
style:{color:'#7f9185',weight:.45,fillColor:'#aebbb0',fillOpacity:.6},
onEachFeature:(f,l)=>{const p=f.properties,box=document.createElement('div');
const height=p.height_conflict?
'整栋取源块最高模型高度：'+p.height_extent_m+' m（源块范围 '+
p.model_height_min_m+'–'+p.model_height_max_m+' m；未经实测核验）':
'模型高度：'+p.height_extent_m+' m（未经实测核验）';
['建筑候选 '+p.id,height,
'轮廓面积：'+Number(p.area_m2).toFixed(1)+' m²',
p.seam_merged?'已合并分缝；原始模型高度按源块最高值赋给整组，建筑实体仍待核':'保持源轮廓',
p.internal_seam_merged?'已合并同一多部件轮廓内部的网格接缝':'',
p.source_component_count>1?'合并了 '+p.source_component_count+' 个源组件':'',
p.remaining_seam_review?'附近仍有疑似分缝待核':'',
p.footprint_requires_review?'轮廓待核':'未标记轮廓问题；不代表已人工核验',
'尚未与 POI 建立逐栋功能对应'].forEach(t=>{
if(!t)return;const el=document.createElement('div');el.textContent=t;box.appendChild(el);});
l.bindPopup(box);}
}).addTo(map);
Object.values(categories).forEach(group=>group.eachLayer(marker=>marker.bringToFront()));
control.addOverlay(layer,BUILDING_LABEL+' · '+data.features.length.toLocaleString());
const mergedCount=data.features.filter(f=>f.properties.seam_merged).length;
const remainingCount=data.features.filter(f=>f.properties.remaining_seam_review).length;
document.getElementById('building-status').textContent=
'已加载 '+data.features.length.toLocaleString()+' 个建筑候选轮廓（灰色）'+
(mergedCount?'，其中 '+mergedCount+' 组已合并分缝。':'，可在右侧开关。')+
(remainingCount?'仍有 '+remainingCount+' 个候选待核。':'重复网格疑似分缝已全部合并。');
const repairedHighlights=L.geoJSON(data,{renderer:buildingRenderer,pane:'buildings',
filter:f=>Boolean(f.properties.seam_merged),
style:{color:'#cb7139',weight:1.5,fill:false},interactive:false});
if(mergedCount)control.addOverlay(repairedHighlights,'标出已合并建筑');
const remaining=L.geoJSON(data,{renderer:buildingRenderer,pane:'buildings',
filter:f=>Boolean(f.properties.remaining_seam_review),
style:{color:'#9f4c8c',weight:1.5,fill:false},interactive:false});
if(remainingCount)control.addOverlay(remaining,'标出仍待核的分缝周边轮廓');
}).catch(()=>document.getElementById('building-status').textContent=
'建筑图层未加载，请刷新重试或查看下方静态图。');
const originalBuildings=L.layerGroup();let originalLoaded=false;
control.addOverlay(originalBuildings,'合并前轮廓（蓝色虚线对照）');
map.on('overlayadd',async event=>{if(event.layer!==originalBuildings||originalLoaded)return;
originalLoaded=true;
try{const response=await fetch('../georeferenced/buildings_wgs84.geojson');
if(!response.ok)throw Error('original');const data=await response.json();
L.geoJSON(data,{renderer:L.canvas({pane:'originalBuildings'}),pane:'originalBuildings',
style:{color:'#3e7bae',weight:1.2,dashArray:'4 3',fill:false},interactive:false})
.addTo(originalBuildings);}catch(e){originalLoaded=false;}});
// Local context keeps the map legible when external map tiles are unavailable.
[['reference_osm_water',{color:'#93b6bf',weight:.5,fillOpacity:.3}],
 ['reference_osm_roads',{color:'#aeb7b1',weight:.5}]].forEach(([file,style])=>{
fetch('../georeferenced/'+file+'_wgs84.geojson').then(r=>r.json()).then(data=>{
const layer=L.geoJSON(data,{renderer:contextRenderer,pane:'context',style,
interactive:false}).addTo(map);
layer.bringToBack();}).catch(()=>{});});
let cached=null;
function draw(){if(!cached)return;
const term=document.getElementById('search').value.trim().toLowerCase();
Object.values(categories).forEach(l=>l.clearLayers());let n=0;
cached.features.forEach(f=>{const p=f.properties,code=p.category_code;
if(term&&!(p.name||'').toLowerCase().includes(term))return;
const layer=categories[code];if(!layer)return;const c=f.geometry.coordinates;
const marker=L.circleMarker([c[1],c[0]],{renderer,radius:4,color:colors[code]||'#666',
weight:.3,fillColor:colors[code]||'#666',fillOpacity:.82});
const box=document.createElement('div'),title=document.createElement('strong');
title.textContent=p.name||'未命名场所';box.appendChild(title);
[p.category,p.subcategory,'类型：'+(roleLabels[p.poi_role]||p.poi_role),
'原始几何：'+p.source_geometry_type,
'合并来源数：'+p.member_count,p.access_raw?'access='+p.access_raw:'开放性未核实'].forEach(t=>{
const el=document.createElement('div');el.textContent=t;box.appendChild(el);});
if(/^osm:(node|way|relation):[0-9]+$/.test(p.osm_uid)){const a=document.createElement('a');
a.href='https://www.openstreetmap.org/'+p.osm_uid.split(':').slice(1).join('/');
a.target='_blank';a.rel='noopener noreferrer';a.textContent='查看 OSM 原始记录';box.appendChild(a);}
marker.bindPopup(box).addTo(layer);n++;});
statusEl.textContent='已加载 '+n.toLocaleString()+' 个匹配名称的点。用右侧类别控制显示。';}
Promise.all([
fetch('poi_map_wgs84.geojson').then(r=>{if(!r.ok)throw Error('POI');return r.json();}),
fetch('../georeferenced/analysis_window_wgs84.geojson').then(r=>r.json())
]).then(([pois,area])=>{cached=pois;const counts={};pois.features.forEach(f=>{
const code=f.properties.category_code;counts[code]=(counts[code]||0)+1;});
Object.keys(counts).sort((a,b)=>counts[b]-counts[a]).forEach(code=>{
const layer=L.layerGroup().addTo(map);categories[code]=layer;
control.addOverlay(layer,'<span class="dot" style="background:'+colors[code]+'"></span>'+
labels[code]+' · '+counts[code]);});
const border=L.geoJSON(area,{style:{color:'#9c6544',weight:2,fill:false}}).addTo(map);
map.fitBounds(border.getBounds());draw();
}).catch(()=>statusEl.textContent='部分数据未载入，请查看下方静态图或下载 GIS。');
document.getElementById('search').addEventListener('input',draw);
document.getElementById('all').onclick=()=>Object.values(categories).forEach(l=>l.addTo(map));
document.getElementById('none').onclick=()=>Object.values(categories).forEach(l=>map.removeLayer(l));
}
</script></html>"""
    text = (
        f"候选 {counts['source_features']:,} · 保留 {counts['kept_unique_pois']:,} · "
        f"剔除 {counts['excluded']:,} · 待核 {counts['review']:,}"
    )
    html = html.replace("COUNTS_TEXT", text).replace("COLORS_JSON", json.dumps(COLORS))
    html = html.replace("LABELS_JSON", json.dumps(CATEGORY_LABELS, ensure_ascii=False))
    html = html.replace(
        "BUILDING_URL",
        json.dumps(
            "../repaired/buildings_wgs84.geojson"
            if use_repaired
            else "../georeferenced/buildings_wgs84.geojson"
        ),
    ).replace("BUILDING_LABEL", json.dumps("修复后建筑轮廓" if use_repaired else "模型建筑轮廓"))
    (root / "map.html").write_text(html)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/suzhou_old_city/osm_pois"))
    render(parser.parse_args().data_dir)
