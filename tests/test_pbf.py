"""OSM extraction must keep untagged multipolygon members and preserve walk filters."""

import importlib.util
from pathlib import Path

import osmnx as ox
import pytest
from shapely.geometry import box

osmium = pytest.importorskip("osmium")
spec = importlib.util.spec_from_file_location(
    "prepare_pbf_sources", Path(__file__).resolve().parents[1] / "scripts/prepare_pbf_sources.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_untagged_building_members_are_reference_complete(tmp_path):
    source = tmp_path / "source.osm"
    source.write_text("""<osm version="0.6" generator="synthetic-test">
<node id="1" lat="32.06" lon="118.77"/>
<node id="2" lat="32.06" lon="118.771"/>
<node id="3" lat="32.061" lon="118.771"/>
<node id="4" lat="32.061" lon="118.77"/>
<way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/></way>
<relation id="100"><member type="way" ref="10" role="outer"/>
<tag k="type" v="multipolygon"/><tag k="building" v="yes"/></relation></osm>""")
    output = tmp_path / "subset.osm"
    area = box(118.769, 32.059, 118.7701, 32.0601)
    with osmium.BackReferenceWriter(output, source, remove_tags=False) as writer:
        collector = module.AreaSelector(writer, area)
        collector.apply_file(source, locations=True)
    features = ox.features_from_xml(output, tags={"building": True})
    assert ("relation", 100) in features.index
    assert features.geometry.iloc[0].area == pytest.approx(0.000001)
    assert features.geometry.iloc[0].is_valid


def test_pbf_walk_filter_tracks_osmnx_preset():
    expression = ox._overpass._get_network_filter("walk")
    assert module.matches_osmnx_filter({"highway": "footway"}, expression)
    assert not module.matches_osmnx_filter({"highway": "motorway"}, expression)
    assert not module.matches_osmnx_filter({"highway": "service", "access": "private"}, expression)
    assert not module.matches_osmnx_filter({"highway": "footway", "foot": "no"}, expression)
    assert not module.matches_osmnx_filter({"building": "yes"}, expression)
    with pytest.raises(ValueError, match="Unsupported"):
        module.matches_osmnx_filter({"highway": "footway"}, '["highway"="footway"]')
