import importlib.util
from pathlib import Path

import pytest
from shapely.geometry import box

pytest.importorskip("osmium")
spec = importlib.util.spec_from_file_location(
    "acquire_osm_pois", Path(__file__).resolve().parents[1] / "scripts/acquire_osm_pois.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_reference_complete_multipolygon_poi_and_raw_unwanted_features(tmp_path):
    source = tmp_path / "source.osm"
    source.write_text("""<osm version="0.6" generator="synthetic-test">
<node id="1" lat="31.30" lon="120.60"/>
<node id="2" lat="31.30" lon="120.601"/>
<node id="3" lat="31.301" lon="120.601"/>
<node id="4" lat="31.301" lon="120.60"/>
<node id="5" lat="31.3005" lon="120.6005"><tag k="natural" v="tree"/></node>
<node id="6" lat="31.3002" lon="120.6002"><tag k="emergency" v="fire_hydrant"/></node>
<node id="7" lat="31.35" lon="120.65"><tag k="shop" v="bakery"/></node>
<way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/></way>
<relation id="100"><member type="way" ref="10" role="outer"/>
<tag k="type" v="multipolygon"/><tag k="amenity" v="school"/>
<tag k="name" v="合成学校"/></relation></osm>""")
    area = box(120.599, 31.299, 120.6011, 31.3011)
    out = tmp_path / "extract"
    out.mkdir()
    result, provenance = module.read_pbf(
        source, area, {"amenity": True, "shop": True, "natural": True, "emergency": True}, out
    )
    identities = set(zip(result.element, result.id.astype(int)))
    assert identities == {("node", 5), ("node", 6), ("relation", 100)}
    assert result[result.element == "relation"].geometry.iloc[0].is_valid
    assert provenance["subset_sha256"]
    assert module.matches_tags({"highway": "bus_stop"}, {"highway": ["bus_stop", "street_lamp"]})
    assert not module.matches_tags({"highway": "primary"}, {"highway": ["bus_stop"]})
