"""Render local processed geometry as a standalone, interactive HTML/SVG map."""

import argparse
import html
import json
import math
from pathlib import Path

from urban_fabric.data import Layers


def path_data(geometry, x0, y1):
    """Convert projected coordinates to SVG coordinates with north pointing up."""
    if geometry.is_empty:
        return ""
    if geometry.geom_type.startswith("Multi") or geometry.geom_type == "GeometryCollection":
        return " ".join(path_data(part, x0, y1) for part in geometry.geoms)
    if geometry.geom_type == "Polygon":
        rings = [geometry.exterior, *geometry.interiors]
        return " ".join(
            "M " + " L ".join(f"{x - x0:.2f},{y1 - y:.2f}" for x, y in ring.coords) + " Z"
            for ring in rings
        )
    return "M " + " L ".join(f"{x - x0:.2f},{y1 - y:.2f}" for x, y in geometry.coords)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path, help="Study-area directory containing processed/")
    args = parser.parse_args()
    root = args.data_dir.resolve()
    layers = Layers.load(root / "processed")
    summary = json.loads((root / "processed/summary.json").read_text(encoding="utf-8"))
    aoi = layers.area.geometry.union_all()
    x0, y0, x1, y1 = aoi.bounds
    width, height = x1 - x0, y1 - y0
    shapes = []
    details = {}
    for index, row in layers.buildings.iterrows():
        key = str(index)
        values = {"OSM 标识": row.id}
        for field, label in [
            ("name", "名称"),
            ("building", "建筑标签"),
            ("area_m2", "完整轮廓面积 / m²"),
            ("height_m", "OSM 高度 / m"),
            ("nearest_street_distance_m", "最近街段几何距离 / m"),
        ]:
            value = row.get(field)
            if isinstance(value, float):
                value = round(value, 2) if math.isfinite(value) else "缺失"
            values[label] = value if value is not None else "缺失"
        values["与研究区边界相交"] = "是" if row.boundary_clipped else "否"
        details[key] = values
        color = "#e7a166" if row.boundary_clipped else "#668b98"
        title = html.escape(f"{row.id} · {row.area_m2:.1f} m²")
        d = path_data(row.geometry, x0, y1)
        shapes.append(
            f'<path d="{d}" fill="{color}" data-building="{key}" '
            f'fill-rule="evenodd"><title>{title}</title></path>'
        )
    roads = "".join(
        f'<path d="{path_data(geometry.intersection(aoi), x0, y1)}"/>'
        for geometry in layers.street_segments.geometry
    )
    junctions = "".join(
        f'<circle cx="{row.geometry.x - x0:.2f}" cy="{y1 - row.geometry.y:.2f}" r="3"/>'
        for _, row in layers.street_nodes.iterrows()
        if row.within_area and row.street_degree >= 3
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" id="map" '
        f'viewBox="-35 -35 {width + 70:.2f} {height + 70:.2f}" '
        'role="img" aria-label="研究区建筑轮廓与街道中心线">'
        f'<g id="buildings" stroke="#fff" stroke-width="0.7">{"".join(shapes)}</g>'
        f'<g id="roads" fill="none" stroke="#596b65" stroke-width="2">{roads}</g>'
        f'<g id="junctions" fill="#c35e45">{junctions}</g>'
        f'<path d="{path_data(aoi, x0, y1)}" fill="none" stroke="#374e45" '
        'stroke-width="2" stroke-dasharray="8 5"/>'
        f'<g transform="translate(35,{height - 15:.2f})" fill="#263e36" '
        'font-family="sans-serif" font-size="13">'
        '<path d="M0,0 h100 M0,-5 v10 M100,-5 v10" stroke="#263e36" stroke-width="2"/>'
        '<text x="0" y="-12">100 m</text></g>'
        '<g transform="translate(35,30)" fill="#263e36" font-family="sans-serif">'
        '<path d="M0,0 l-6,18 6,-4 6,4 Z"/><text x="-5" y="-7">N</text></g></svg>'
    )
    quality = summary["quality"]
    title = html.escape(summary["study_area_id"])
    page = r"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ · 城市形态样例</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f6f5ee;color:#263e36;
font:15px/1.65 system-ui,-apple-system,sans-serif}main{display:grid;
grid-template-columns:340px 1fr;min-height:100vh}aside{padding:30px;background:#fffefa;
border-right:1px solid #dce2d8}h1{font-size:26px;line-height:1.3;margin:12px 0}
small,.muted{color:#687c70}p{margin:12px 0}dl{display:grid;grid-template-columns:1fr 1fr;
gap:12px;margin:24px 0}dt{font-size:12px;color:#687c70}dd{font-size:23px;margin:0}
label{display:block;padding:4px 0}button{font:inherit;border:1px solid #b9c9bf;
background:#fffefa;color:#263e36;border-radius:6px;padding:4px 14px;cursor:pointer}
.canvas{position:relative;overflow:hidden}svg{width:100%;height:100vh;display:block;
touch-action:none;cursor:grab}svg:active{cursor:grabbing}[data-building]{cursor:pointer}
[data-selected="true"]{fill:#e9c04f!important;stroke:#67551d;stroke-width:2}
#detail{font-size:12px;word-break:break-all;white-space:pre-line;margin-top:18px}
.foot{position:absolute;right:14px;bottom:10px;font-size:11px;background:#fffefae6;
padding:3px 10px;pointer-events:none}a{color:#436d71}
@media(max-width:760px){main{grid-template-columns:1fr}aside{padding:20px}
svg{height:70vh}dl{grid-template-columns:repeat(4,1fr)}dd{font-size:19px}}
</style><main><aside><small>URBAN FABRIC LAB / 真实 OSM 样例</small>
<h1>__HEADING__ · 建筑与街道</h1><p class="muted">__TITLE__<br>范围：__LONGITUDE__°E<br>
__LATITUDE__°N（WGS84）</p><dl>
<div><dt>研究区 / km²</dt><dd>__AREA__</dd></div>
<div><dt>建筑轮廓</dt><dd>__BUILDINGS__</dd></div>
<div><dt>建筑覆盖率</dt><dd>__COVERAGE__%</dd></div>
<div><dt>物理街段</dt><dd>__STREETS__</dd></div></dl>
<label><input type="checkbox" data-layer="buildings" checked> 建筑 · 蓝色；边界建筑 · 橙色</label>
<label><input type="checkbox" data-layer="roads" checked> 街道中心线 · 灰绿色</label>
<label><input type="checkbox" data-layer="junctions" checked> 度数 ≥ 3 的路口 · 红色</label>
<p><button id="reset">重置视图</button></p>
<p class="muted">滚轮缩放、拖动平移、点击建筑查看指标。比例尺随地图缩放。</p>
<div id="detail">点击一个建筑，查看 OSM 标识、面积及最近街段距离。</div>
<p><small>__MISSING__ 个建筑缺少高度；__UNMATCHED__ 个建筑在 __DISTANCE__ m 内未匹配到街段。
几何邻近不代表入口可达。边界建筑保留完整轮廓，覆盖率只统计区内部分。</small></p>
<p><small>采集：__DATE__ UTC<br>投影：__CRS__<br>来源：
<a href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors · ODbL</a>
</small></p>
</aside><section class="canvas">__SVG__<div class="foot">本地几何预览 · 无需联网</div></section>
</main><script>
const details=__DETAILS__,svg=document.getElementById('map');
const initial=svg.getAttribute('viewBox').split(' ').map(Number);let view=[...initial],drag=null;
function draw(){svg.setAttribute('viewBox',view.join(' '))}
function at(e){const p=svg.createSVGPoint();p.x=e.clientX;p.y=e.clientY;
return p.matrixTransform(svg.getScreenCTM().inverse())}
svg.addEventListener('wheel',e=>{e.preventDefault();const p=at(e),f=e.deltaY>0?1.15:1/1.15;
if(view[2]*f<initial[2]/20||view[2]*f>initial[2]*3)return;
view=[p.x+(view[0]-p.x)*f,p.y+(view[1]-p.y)*f,view[2]*f,view[3]*f];draw()},{passive:false});
svg.addEventListener('pointerdown',e=>{drag={x:e.clientX,y:e.clientY,view:[...view],
matrix:svg.getScreenCTM().inverse(),moved:false};svg.setPointerCapture(e.pointerId)});
svg.addEventListener('pointermove',e=>{if(!drag)return;
const dx=e.clientX-drag.x,dy=e.clientY-drag.y;
if(Math.hypot(dx,dy)>4)drag.moved=true;
view[0]=drag.view[0]-dx*drag.matrix.a-dy*drag.matrix.c;
view[1]=drag.view[1]-dx*drag.matrix.b-dy*drag.matrix.d;draw()});
svg.addEventListener('pointerup',e=>{if(drag&&!drag.moved){
const element=document.elementFromPoint(e.clientX,e.clientY)?.closest('[data-building]');
if(element){document.querySelector('[data-selected]')?.removeAttribute('data-selected');
element.setAttribute('data-selected','true');
document.getElementById('detail').textContent=Object.entries(details[element.dataset.building])
.map(([key,value])=>key+'：'+value).join('\n')}}drag=null});
svg.addEventListener('pointercancel',()=>{drag=null});
document.getElementById('reset').onclick=()=>{view=[...initial];draw()};
document.querySelectorAll('[data-layer]').forEach(input=>input.onchange=()=>{
document.getElementById(input.dataset.layer).style.display=input.checked?'':'none'});
</script></html>"""
    west, south, east, north = layers.area.to_crs(4326).total_bounds
    replacements = {
        "__TITLE__": title,
        "__HEADING__": title,
        "__LONGITUDE__": f"{west:.3f}–{east:.3f}",
        "__LATITUDE__": f"{south:.3f}–{north:.3f}",
        "__AREA__": f"{summary['area_m2'] / 1e6:.3f}",
        "__BUILDINGS__": str(summary["building_count"]),
        "__COVERAGE__": f"{summary['building_coverage_ratio'] * 100:.2f}",
        "__STREETS__": str(summary["street_segments"]),
        "__MISSING__": str(quality["missing_height_count"]),
        "__UNMATCHED__": str(quality["buildings_without_near_street"]),
        "__DISTANCE__": f"{summary['parameters']['nearest_street_max_distance_m']:g}",
        "__DATE__": summary["source_manifest"]["acquired_at_utc"].split(".")[0],
        "__CRS__": html.escape(summary["metric_crs"]),
        "__SVG__": svg,
        "__DETAILS__": json.dumps(details, ensure_ascii=False, allow_nan=False).replace(
            "</", "<\\/"
        ),
    }
    for key, value in replacements.items():
        page = page.replace(key, value)
    destination = root / "preview"
    destination.mkdir(exist_ok=True)
    (destination / "index.html").write_text(page, encoding="utf-8")
    (destination / "overview.svg").write_text(svg, encoding="utf-8")
    print(destination / "index.html")


if __name__ == "__main__":
    main()
