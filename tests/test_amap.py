import json
import urllib.error

import pytest

from urban_fabric.amap import AmapClient, AmapError

RING = [(120.6, 31.3), (120.61, 31.3), (120.61, 31.31), (120.6, 31.3)]


def test_official_conversion_and_count_is_page_count(tmp_path):
    calls = []

    def transport(endpoint, params):
        calls.append((endpoint, params))
        if "convert" in endpoint:
            assert params["coordsys"] == "gps"
            return {"status": "1", "locations": "120.604,31.298;120.614,31.308"}
        if params["page_num"] == "1":
            return {"status": "1", "count": "2", "pois": [{"id": "a"}, {"id": "b"}]}
        return {"status": "1", "count": "1", "pois": [{"id": "b"}]}

    client = AmapClient("private-test-key", tmp_path, interval=0, transport=transport)
    assert client.wgs84_to_gcj02([(120.6, 31.3), (120.61, 31.31)]) == [
        (120.604, 31.298),
        (120.614, 31.308),
    ]
    pois, manifest = client.polygon_sample(RING, types="140000", max_pages=3, page_size=2)
    assert [p["id"] for p in pois] == ["a", "b"]
    assert len(calls) == 3  # count=2 on page 1 is not a total requiring only one page.
    assert manifest["coordinate_reference"] == "GCJ-02 (Amap); NOT EPSG:4326"
    assert manifest["completeness_not_guaranteed"]
    for path in tmp_path.glob("*.json"):
        assert "private-test-key" not in path.read_text()


def test_request_budget_and_api_cap(tmp_path):
    def transport(endpoint, params):
        page = int(params["page_num"])
        return {"status": "1", "pois": [{"id": str(page * 25 + i)} for i in range(25)]}

    client = AmapClient(
        "private-test-key", tmp_path, interval=0, transport=transport, max_requests=10
    )
    pois, manifest = client.polygon_sample(RING, types="140000", max_pages=8)
    assert len(pois) == 200
    assert client.calls == 8
    assert manifest["stop_reason"] == "api_200_record_cap"
    client = AmapClient(
        "private-test-key", tmp_path / "budget", interval=0, transport=transport, max_requests=1
    )
    _, manifest = client.polygon_sample(RING, types="140000", max_pages=8)
    assert client.calls == 1
    assert manifest["stop_reason"] == "request_budget"


def test_transport_errors_and_echoed_responses_redact_key(tmp_path):
    def failure(endpoint, params):
        raise urllib.error.URLError("https://example?key=private-test-key")

    client = AmapClient("private-test-key", tmp_path, interval=0, transport=failure)
    with pytest.raises(AmapError) as error:
        client.wgs84_to_gcj02([(120.6, 31.3)])
    assert "private-test-key" not in str(error.value)

    def echo(endpoint, params):
        return {"status": "0", "info": "error private-test-key", "infocode": "10001"}

    client = AmapClient("private-test-key", tmp_path, interval=0, transport=echo)
    with pytest.raises(AmapError) as error:
        client.wgs84_to_gcj02([(120.6, 31.3)])
    assert "private-test-key" not in str(error.value)
    assert "private-test-key" not in json.dumps(
        json.loads((tmp_path / "response_001.json").read_text())
    )
