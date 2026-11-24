"""Create an offline Canvas map colored by predicted GBA building height."""

import argparse
import html
import json
import math
from pathlib import Path

import geopandas as gpd


def render(root):
    summary = json.loads((root / "processed/summary.json").read_text(encoding="utf-8"))
    buildings = gpd.read_parquet(root / "processed/buildings.parquet")
    area = gpd.read_parquet(root / "processed/area.parquet").geometry.iloc[0]
    x0, y0, x1, y1 = area.bounds

    def polygons(geometry):
        return list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]

    def rings(geometry):
        return [
            [[round(x - x0, 2), round(y1 - y, 2)] for x, y, *_ in ring.coords]
            for polygon in polygons(geometry)
            for ring in [polygon.exterior, *polygon.interiors]
        ]

    def scalar(value):
        return round(float(value), 3) if value is not None and math.isfinite(value) else None

    rows = [
        {
            "id": row.id,
            "h": scalar(row.height_m),
            "v": scalar(row.gba_var_raw),
            "a": round(row.area_m2, 2),
            "source": row.polygon_source,
            "border": bool(row.boundary_intersects),
            "near_street_m": scalar(getattr(row, "nearest_street_distance_m", None)),
            "rings": rings(row.geometry),
        }
        for row in buildings.itertuples()
    ]

    def line_parts(geometry):
        if geometry.is_empty:
            return []
        if geometry.geom_type == "LineString":
            return [[[round(x - x0, 2), round(y1 - y, 2)] for x, y, *_ in geometry.coords]]
        if hasattr(geometry, "geoms"):
            return [line for part in geometry.geoms for line in line_parts(part)]
        return []

    streets_path = root / "processed/street_segments.parquet"
    roads = []
    junctions = []
    if streets_path.exists():
        streets = gpd.read_parquet(streets_path).to_crs(buildings.crs)
        for row in streets.itertuples():
            roads.append(
                {
                    "id": row.id,
                    "lines": line_parts(row.geometry.intersection(area)),
                    "walk": any(
                        kind in str(row.highway)
                        for kind in ("footway", "pedestrian", "path", "steps")
                    ),
                    "name": row.name if isinstance(row.name, str) else "",
                }
            )
        nodes = gpd.read_parquet(root / "processed/street_nodes.parquet").to_crs(buildings.crs)
        junctions = [
            [round(row.geometry.x - x0, 2), round(y1 - row.geometry.y, 2)]
            for row in nodes.itertuples()
            if row.within_area and row.street_degree >= 3
        ]
    data = {
        "rows": rows,
        "boundary": rings(area),
        "width": x1 - x0,
        "height": y1 - y0,
        "roads": roads,
        "junctions": junctions,
    }
    page = r"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__NAME__ · GBA 建筑高度</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f4f5f0;color:#253d37;
font:14px/1.65 system-ui,-apple-system,sans-serif}main{display:grid;
grid-template-columns:350px minmax(0,1fr);min-height:100vh}aside{padding:28px;
background:#fffef9;border-right:1px solid #d8e0d8}h1{font-size:26px;line-height:1.35}
.muted,small{color:#6a7c72}.stats{display:grid;grid-template-columns:1fr 1fr;gap:14px;
margin:24px 0}.number{display:block;font-size:24px}.label{font-size:12px;color:#6a7c72}
.legend label{display:flex;align-items:center;gap:8px;margin:7px 0;cursor:pointer}
.swatch{display:inline-block;width:20px;height:12px;border-radius:2px}.count{margin-left:auto;
color:#6a7c72}button{font:inherit;background:#fffef9;border:1px solid #bfcbc2;color:#253d37;
border-radius:5px;padding:5px 14px;cursor:pointer}.map{position:relative;min-height:70vh}
canvas{width:100%;height:100vh;display:block;touch-action:none;cursor:grab}
canvas:active{cursor:grabbing}#detail{font-size:12px;white-space:pre-line;word-break:break-all;
min-height:125px;margin:18px 0}.badge{position:absolute;bottom:12px;right:14px;
background:#fffef9e8;padding:3px 8px;font-size:11px}a{color:#426e72}
@media(max-width:760px){main{grid-template-columns:1fr}aside{padding:20px}
canvas{height:75vh}.stats{grid-template-columns:repeat(4,1fr)}.number{font-size:20px}}
</style><main><aside><small>URBAN FABRIC LAB / GlobalBuildingAtlas</small>
<h1>__NAME__<br>建筑高度分布</h1><p class="muted">__SCOPE__ · 建筑轮廓保留完整几何<br>
高度为机器学习估计，未经本地实测校核。</p><div class="stats">
<div><span class="label">区界面积 / km²</span><span class="number">__AREA__</span></div>
<div><span class="label">样本建筑轮廓</span><span class="number">__COUNT__</span></div>
<div><span class="label">样本高度有值率</span><span class="number">__COVERAGE__%</span></div>
<div><span class="label">高度中位数 / m</span><span class="number">__MEDIAN__</span></div>
</div><div class="legend" id="legend"></div>
<div class="legend" id="network" __NETWORK_HIDDEN__>
<label><input id="show-roads" type="checkbox" checked>
街道与步行通道<span class="count">__ROADS__ 条街段</span></label>
<label><input id="show-junctions" type="checkbox">
路口 · 红点<span class="count">__JUNCTIONS__</span></label></div>
<p><button id="reset">重置视图</button></p>
<small>滚轮缩放、拖动平移、点击建筑查看属性。图例为展示区间，可点击隐藏；不代表法定建筑分类。</small>
<div id="detail">点击建筑查看估计高度、原始来源及完整轮廓面积。</div>
<p><a href="../processed/building_heights.csv" download>下载高度 CSV</a></p>
<p><small>边界：© OpenStreetMap contributors · ODbL<br>
建筑：GlobalBuildingAtlas，Zhu et al. (2025)<br>轮廓分部许可：ODbL / CC BY-NC 4.0；
高度：CC BY-NC 4.0。<br>__LIMITATION__<br>投影：__CRS__<br>采集：__DATE__ UTC<br>
__ROAD_NOTE__
<a href="https://essd.copernicus.org/articles/17/6647/2025/">数据论文与方法</a></small></p>
</aside><section class="map"><canvas id="map" aria-label="建筑估计高度分布地图"></canvas>
<div class="badge">本地离线地图 · 高度单位：m</div></section></main><script>
const data=__DATA__,canvas=document.getElementById('map'),ctx=canvas.getContext('2d');
const hit=document.createElement('canvas').getContext('2d');
const colors=['#c6d9c4','#87b5a6','#4c8d92','#416d89','#744f80','#d6d5ce'];
const labels=['0 < h < 10 m','10 ≤ h < 24 m','24 ≤ h < 54 m',
'54 ≤ h < 100 m','h ≥ 100 m','缺失高度'];
const enabled=colors.map(()=>true),counts=colors.map(()=>0);
function category(h){return h===null?5:h<10?0:h<24?1:h<54?2:h<100?3:4}
function path(rings){let p=new Path2D();for(const ring of rings){
ring.forEach(([x,y],i)=>i?p.lineTo(x,y):p.moveTo(x,y));p.closePath()}return p}
const shapes=data.rows.map(row=>{row.cat=category(row.h);counts[row.cat]++;
const xs=row.rings.flat().map(p=>p[0]),ys=row.rings.flat().map(p=>p[1]);
row.bounds=[Math.min(...xs),Math.min(...ys),Math.max(...xs),Math.max(...ys)];
return path(row.rings)});
const boundary=path(data.boundary);let center=[data.width/2,data.height/2],zoom=1;
function linePath(lines){const p=new Path2D();for(const line of lines){
line.forEach(([x,y],i)=>i?p.lineTo(x,y):p.moveTo(x,y))}return p}
const roadShapes=data.roads.map(row=>linePath(row.lines));
let showRoads=true,showJunctions=false;
let width=0,height=0,scale=1,drag=null,selected=-1,queued=false;
function screenToWorld(x,y){return [(x-width/2)/scale+center[0],(y-height/2)/scale+center[1]]}
function draw(){queued=false;const ratio=devicePixelRatio||1;
width=canvas.clientWidth;height=canvas.clientHeight;
if(canvas.width!==Math.round(width*ratio)||canvas.height!==Math.round(height*ratio)){
canvas.width=Math.round(width*ratio);canvas.height=Math.round(height*ratio)}
scale=Math.min((width-50)/data.width,(height-50)/data.height)*zoom;
ctx.setTransform(ratio,0,0,ratio,0,0);ctx.fillStyle='#f4f5f0';ctx.fillRect(0,0,width,height);
ctx.translate(width/2-center[0]*scale,height/2-center[1]*scale);ctx.scale(scale,scale);
ctx.fillStyle='#e8eee5';ctx.fill(boundary,'evenodd');
if(showRoads){ctx.lineCap='round';
for(let i=0;i<roadShapes.length;i++){ctx.strokeStyle=data.roads[i].walk?'#9baca0':'#87978d';
ctx.lineWidth=(data.roads[i].walk?.8:1.4)/scale;ctx.stroke(roadShapes[i])}}
const [left,top]=screenToWorld(0,0),[right,bottom]=screenToWorld(width,height);
for(let i=0;i<shapes.length;i++){const row=data.rows[i],b=row.bounds;
if(!enabled[row.cat]||b[2]<left||b[0]>right||b[3]<top||b[1]>bottom)continue;
ctx.fillStyle=colors[row.cat];ctx.fill(shapes[i],'evenodd');
if(zoom>4){ctx.strokeStyle='#ffffffaa';ctx.lineWidth=.4/scale;ctx.stroke(shapes[i])}}
if(selected>=0&&enabled[data.rows[selected].cat]){ctx.strokeStyle='#db613b';
ctx.lineWidth=2.5/scale;ctx.stroke(shapes[selected])}
if(showJunctions){ctx.fillStyle='#bd644c';for(const [x,y] of data.junctions){
ctx.beginPath();ctx.arc(x,y,2.3/scale,0,Math.PI*2);ctx.fill()}}
ctx.strokeStyle='#506658';ctx.lineWidth=1.2/scale;ctx.setLineDash([5/scale,4/scale]);
ctx.stroke(boundary);ctx.setLineDash([]);ctx.setTransform(ratio,0,0,ratio,0,0);
const desired=100/scale,power=10**Math.floor(Math.log10(desired));
const metres=[1,2,5,10].map(n=>n*power).find(n=>n>=desired)||power*10;
ctx.strokeStyle='#253d37';ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(25,height-30);
ctx.lineTo(25+metres*scale,height-30);ctx.stroke();ctx.font='12px system-ui';
ctx.fillStyle='#253d37';ctx.fillText(metres>=1000?(metres/1000)+' km':metres+' m',25,height-39);
ctx.fillText('N ↑',25,30)}
function requestDraw(){if(!queued){queued=true;requestAnimationFrame(draw)}}
function position(e){const r=canvas.getBoundingClientRect();
return [e.clientX-r.left,e.clientY-r.top]}
canvas.addEventListener('wheel',e=>{e.preventDefault();
const [x,y]=position(e),before=screenToWorld(x,y);
zoom=Math.min(100,Math.max(.6,zoom*(e.deltaY<0?1.18:1/1.18)));
scale=Math.min((width-50)/data.width,(height-50)/data.height)*zoom;
const after=screenToWorld(x,y);center=[center[0]+before[0]-after[0],center[1]+before[1]-after[1]];
requestDraw()},{passive:false});
canvas.onpointerdown=e=>{drag={x:e.clientX,y:e.clientY,center:[...center],moved:false};
canvas.setPointerCapture(e.pointerId)};
canvas.onpointermove=e=>{if(!drag)return;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;
if(Math.hypot(dx,dy)>4)drag.moved=true;center=[drag.center[0]-dx/scale,drag.center[1]-dy/scale];
requestDraw()};
canvas.onpointerup=e=>{if(drag&&!drag.moved){const [x,y]=screenToWorld(...position(e));
for(let i=shapes.length-1;i>=0;i--){const row=data.rows[i],b=row.bounds;
if(enabled[row.cat]&&x>=b[0]&&x<=b[2]&&y>=b[1]&&y<=b[3]&&hit.isPointInPath(shapes[i],x,y,'evenodd')){
selected=i;document.getElementById('detail').textContent=[row.id,
'估计高度：'+(row.h===null?'缺失':row.h+' m'),'完整轮廓面积：'+row.a+' m²',
'轮廓原始来源：'+row.source,'原始 var：'+(row.v??'缺失'),
'最近街段几何距离：'+(row.near_street_m===null?'未匹配':row.near_street_m+' m'),
'跨越区界：'+(row.border?'是':'否')].join('\n');requestDraw();break}}}drag=null};
canvas.onpointercancel=()=>{drag=null};
document.getElementById('reset').onclick=()=>{center=[data.width/2,data.height/2];zoom=1;requestDraw()};
document.getElementById('show-roads').onchange=e=>{showRoads=e.target.checked;requestDraw()};
document.getElementById('show-junctions').onchange=e=>{showJunctions=e.target.checked;requestDraw()};
colors.forEach((color,i)=>{const label=document.createElement('label');
const box=document.createElement('input');box.type='checkbox';box.checked=true;
box.onchange=()=>{enabled[i]=box.checked;requestDraw()};
const swatch=document.createElement('span');swatch.className='swatch';swatch.style.background=color;
const count=document.createElement('span');count.className='count';
count.textContent=counts[i].toLocaleString();
label.append(box,swatch,document.createTextNode(labels[i]),count);
document.getElementById('legend').append(label)});
new ResizeObserver(requestDraw).observe(canvas);requestDraw();
</script></html>"""
    replacements = {
        "__NAME__": html.escape(summary["name"]),
        "__SCOPE__": (
            "选定范围内的部分建筑样本"
            if summary.get("partial_building_sample")
            else "按选定范围筛选"
        ),
        "__LIMITATION__": (
            "仅使用 GBA 的 ODbL 轮廓分部，不代表范围内建筑总量。"
            if summary.get("partial_building_sample")
            else "OSM 边界未经测绘核验。"
        ),
        "__AREA__": f"{summary['area_km2']:.2f}",
        "__NETWORK_HIDDEN__": "" if roads else 'style="display:none"',
        "__ROADS__": str(len(roads)),
        "__JUNCTIONS__": str(len(junctions)),
        "__ROAD_NOTE__": ("路网：OSM · ODbL；邻近距离不代表实际入口可达。<br>" if roads else ""),
        "__COUNT__": f"{len(buildings):,}",
        "__COVERAGE__": f"{summary['height_coverage_ratio'] * 100:.2f}",
        "__MEDIAN__": (
            f"{summary['height_quantiles_m']['0.5']:.1f}"
            if summary["height_quantiles_m"]
            else "缺失"
        ),
        "__CRS__": html.escape(summary["metric_crs"]),
        "__DATE__": summary["source_manifest"]["acquired_at_utc"].split(".")[0],
        "__DATA__": json.dumps(
            data, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).replace("</", "<\\/"),
    }
    for key, value in replacements.items():
        page = page.replace(key, value)
    destination = root / "preview/index.html"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(page, encoding="utf-8")
    print(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    render(parser.parse_args().data_dir.resolve())
