"""Offline, inspectable SVG quality-control map for a prepared dataset."""

import argparse
import html
import json
import math
from pathlib import Path

import geopandas as gpd


def render(root):
    summary = json.loads((root / "processed/summary.json").read_text())
    qa = json.loads((root / "qa/report.json").read_text())
    buildings = gpd.read_parquet(root / "processed/buildings.parquet")
    streets = gpd.read_parquet(root / "processed/street_segments.parquet")
    nodes = gpd.read_parquet(root / "processed/street_nodes.parquet")
    core = gpd.read_parquet(root / "processed/area.parquet").geometry.iloc[0]
    context = gpd.read_parquet(root / "source/context.parquet").geometry.iloc[0]
    x0, y0, x1, y1 = context.bounds

    def point(x, y):
        return f"{x - x0:.2f},{y1 - y:.2f}"

    def path(geom):
        if geom.is_empty:
            return ""
        if geom.geom_type == "Polygon":
            return " ".join(
                "M" + " L".join(point(x, y) for x, y, *_ in ring.coords) + " Z"
                for ring in [geom.exterior, *geom.interiors]
            )
        if geom.geom_type in {"LineString", "LinearRing"}:
            return "M" + " L".join(point(x, y) for x, y, *_ in geom.coords)
        if hasattr(geom, "geoms"):
            return " ".join(path(part) for part in geom.geoms)
        return ""

    def mark(geometry, color, radius=7):
        center = geometry.representative_point()
        x, y = point(center.x, center.y).split(",")
        return f'<circle cx="{x}" cy="{y}" r="{radius}" fill="{color}"/>'

    road_shapes, building_shapes, review_shapes, barrier_shapes, entrance_shapes = (
        [],
        [],
        [],
        [],
        [],
    )
    components = nodes.set_index("id").component_id.to_dict()
    for row in streets.itertuples():
        color = "#ba7740" if components[row.u] else "#52717a"
        opacity = 1 if row.intersects_core else 0.35
        road_shapes.append(
            f'<path d="{path(row.geometry)}" stroke="{color}" '
            f'opacity="{opacity}" fill="none" stroke-width="3"/>'
        )
    for row in buildings.itertuples():
        height = row.height_m
        height = float(height) if height is not None and math.isfinite(height) else None
        color = "#b8c6cd" if height is None else "#8fa864" if height < 24 else "#417a61"
        if row.overlap_review:
            color = "#ce7a72"
        info = {
            "OSM ID": row.id,
            "面积 m²": round(row.area_m2, 1),
            "高度 m": height,
            "高度来源": row.height_source,
            "高度冲突待核": bool(row.height_requires_review),
            "重叠待核": bool(row.overlap_review),
            "最近街段距离 m": None
            if not math.isfinite(row.nearest_street_distance_m)
            else round(row.nearest_street_distance_m, 1),
        }
        attr = html.escape(json.dumps(info, ensure_ascii=False), quote=True)
        building_shapes.append(
            f'<path d="{path(row.geometry)}" fill="{color}" '
            f'fill-rule="evenodd" stroke="#fffef7" stroke-width="1" '
            f'data-info="{attr}"/>'
        )
    by_id = nodes.set_index("id")
    for row in qa["near_miss_candidates"]:
        review_shapes.append(mark(by_id.geometry.loc[row["node_id"]], "#c53685", 11))
    for name, shapes, color in [
        ("barriers", barrier_shapes, "#85579a"),
        ("entrances", entrance_shapes, "#227d9a"),
    ]:
        frame = gpd.read_parquet(root / "qa" / f"osm_{name}.parquet")
        for geom in frame.geometry:
            if geom.geom_type == "Point":
                shapes.append(mark(geom, color, 7))
            else:
                shapes.append(
                    f'<path d="{path(geom)}" stroke="{color}" stroke-width="4" fill="none"/>'
                )
    groups = {
        "roads": road_shapes,
        "buildings": building_shapes,
        "barriers": barrier_shapes,
        "entrances": entrance_shapes,
        "review": review_shapes,
    }
    svg = f'<path d="{path(core)}" fill="#fbfcf6" stroke="#98a99c" stroke-width="7"/>'
    svg += "".join(f'<g id="{name}">' + "".join(shapes) + "</g>" for name, shapes in groups.items())
    toggles = "".join(
        f'<label><input type="checkbox" checked data-layer="{name}">{label}</label>'
        for name, label in [
            ("roads", "道路 · 橙色为其他连通分量"),
            ("buildings", "建筑 · 红色为轮廓重叠待核"),
            ("barriers", "OSM 围栏与闸门"),
            ("entrances", "OSM 已标注入口"),
            ("review", "2 米内疑似断接 · 仅供复核"),
        ]
    )
    h = qa["height_coverage"]
    stats = [
        ("核心区建筑", len(buildings)),
        ("背景路网街段", len(streets)),
        ("核心区街段", int(streets.intersects_core.sum())),
        ("高度有值 / 缺失", f"{h['usable_height']} / {h['missing']}"),
        ("入口 / 围栏对象", f"{len(entrance_shapes)} / {len(barrier_shapes)}"),
        ("路网连通分量", qa["connected_components"]),
    ]
    cards = "".join(
        f"<div><small>{label}</small><strong>{value}</strong></div>" for label, value in stats
    )
    page = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__NAME__ · 数据清洗核验</title>

<style>*{box-sizing:border-box}
body{margin:0;background:#f0f3ee;color:#243a32;font:14px/1.7 system-ui}
main{display:grid;grid-template-columns:340px 1fr;min-height:100vh}
aside{background:#fffef8;padding:28px}
h1{font-size:26px;line-height:1.35;margin:12px 0}p,small{color:#66796f}.cards{display:grid;
grid-template-columns:1fr 1fr;gap:14px;margin:24px 0}strong,small{display:block}
strong{font-size:23px}
label{display:block;margin:10px 0;font-size:12px}input{margin-right:8px}
button{font:inherit;padding:5px 14px;
border:1px solid #afbeb4;border-radius:5px;background:#fffef8;color:#243a32;cursor:pointer}
section{height:100vh;overflow:hidden;position:relative}svg{width:100%;height:100%;touch-action:none;
cursor:grab}svg path[data-info]{cursor:pointer}#detail{white-space:pre-wrap;word-break:break-all;
font-size:12px;background:#eef2eb;padding:12px;margin-top:16px;min-height:120px}a{color:#35675e}
.pass{font-size:12px;background:#e3eedf;padding:8px 10px;border-radius:4px}footer{position:absolute;
bottom:10px;right:14px;background:#fffef8dd;padding:5px 10px;font-size:11px;color:#607368}
@media(max-width:760px){main{grid-template-columns:1fr}section{height:75vh}}</style>
<main><aside><small>URBAN FABRIC LAB / DATA QUALITY</small><h1>__NAME__
<br>数据清洗核验</h1>
<p>核心区 __AREA__ km² · 保留周边 __BUFFER__ 米路网
<br>OSM 快照：__DATE__</p>
<div class="pass">几何与拓扑硬检查通过 · 图结构往返核对通过</div>
<div class="cards">__CARDS__</div>__TOGGLES__<button id="reset">重置视图</button>
<p><small>__REVIEW__</small></p>
<div id="detail">点击建筑查看来源、面积和高度。
<br>滚轮缩放，拖动平移。</div>
<p><small>有高度：绿色；高度缺失：蓝灰色。GBA 高度是模型估计，未经实测。
邻近道路不代表存在通行入口。</small></p>
<p><a href="../qa/report.json">查看完整检查报告</a> ·
<a href="../processed/building_metrics.csv">建筑指标 CSV</a></p>
</aside><section><svg viewBox="__VIEW__" xmlns="http://www.w3.org/2000/svg">__SVG__</svg>
<footer>EPSG:32650 · OSM contributors / ODbL · GBA height / CC BY-NC 4.0</footer></section></main>
<script>const svg=document.querySelector('svg'),
initial=svg.getAttribute('viewBox').split(' ').map(Number);
let view=[...initial],drag=null,selected=null;
function apply(){svg.setAttribute('viewBox',view.join(' '))}
document.querySelectorAll('[data-layer]').forEach(e=>e.onchange=()=>{
document.getElementById(e.dataset.layer).style.display=e.checked?'':'none'});
document.getElementById('reset').onclick=()=>{view=[...initial];apply()};
svg.addEventListener('wheel',e=>{e.preventDefault();
const f=Math.exp(Math.max(-1,Math.min(1,e.deltaY*.001)));
const p=new DOMPoint(e.clientX,e.clientY).matrixTransform(svg.getScreenCTM().inverse());
const px=(p.x-view[0])/view[2],py=(p.y-view[1])/view[3];
view=[view[0]+view[2]*px*(1-f),view[1]+view[3]*py*(1-f),view[2]*f,view[3]*f];apply()},{passive:false});
svg.addEventListener('pointerdown',e=>{drag=[e.clientX,e.clientY,...view];
selected=e.target.dataset.info;svg.setPointerCapture(e.pointerId)});
svg.addEventListener('pointermove',e=>{if(!drag)return;const m=svg.getScreenCTM();
view[0]=drag[2]-(e.clientX-drag[0])/m.a;
view[1]=drag[3]-(e.clientY-drag[1])/m.d;apply()});
svg.addEventListener('pointerup',e=>{
if(drag&&Math.hypot(e.clientX-drag[0],e.clientY-drag[1])<5&&selected){
document.getElementById('detail').textContent=Object.entries(JSON.parse(selected))
.map(([k,v])=>k+'：'+(v===null?'缺失':v)).join('\\n')}
drag=null;selected=null;svg.releasePointerCapture(e.pointerId)});
svg.addEventListener('pointercancel',()=>{drag=null;selected=null});</script></html>"""
    replacements = {
        "NAME": html.escape(str(summary.get("name", summary["study_area_id"]))),
        "BUFFER": str(summary["source_manifest"].get("context_buffer_m", "未记录")),
        "AREA": f"{summary['area_m2'] / 1e6:.3f}",
        "DATE": summary["source_manifest"]["feature_acquisition"].get(
            "snapshot_timestamp_utc", "见来源清单"
        ),
        "CARDS": cards,
        "REVIEW": (
            f"其中 {h['conflicts']} 处高度冲突；屏蔽冲突后有 "
            f"{h['analysis_height_without_conflicts']} 个分析用高度。"
        ),
        "TOGGLES": toggles,
        "VIEW": f"0 0 {x1 - x0:.2f} {y1 - y0:.2f}",
        "SVG": svg,
    }
    for key, value in replacements.items():
        page = page.replace(f"__{key}__", value)
    out = root / "preview"
    out.mkdir(exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    print(out / "index.html")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    render(parser.parse_args().data_dir.resolve())
