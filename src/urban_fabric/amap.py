"""Small, bounded Amap v5 POI samples using the official coordinate conversion API."""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .data import utc_now, write_json


class AmapError(RuntimeError):
    pass


class AmapClient:
    def __init__(self, key, output_dir: Path, *, max_requests=4, interval=1.0, transport=None):
        if not key:
            raise ValueError("请在本地 .env 设置 AMAP_WEB_KEY（Web 服务类型）。")
        if max_requests < 1 or interval < 0:
            raise ValueError("Invalid request budget or interval.")
        self._key = key
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_requests = max_requests
        self.interval = interval
        self.calls = 0
        self._last_call = None
        self.transport = transport or self._http

    @staticmethod
    def _http(endpoint, params):
        url = endpoint + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.load(response)

    def request(self, path, params):
        if self.calls >= self.max_requests:
            raise AmapError("Request budget reached; no further calls made.")
        if self._last_call is not None:
            time.sleep(max(0, self.interval - (time.monotonic() - self._last_call)))
        self.calls += 1
        self._last_call = time.monotonic()
        try:
            result = self.transport("https://restapi.amap.com" + path, {**params, "key": self._key})
            # Some errors echo request parameters. Keys must not enter saved responses or errors.
            result = json.loads(json.dumps(result).replace(self._key, "[REDACTED]"))
        except Exception:
            raise AmapError(
                "高德请求失败；请核对网络和控制台。请求 URL 与密钥未写入日志。"
            ) from None
        write_json(
            self.output_dir / f"response_{self.calls:03}.json",
            {
                "fetched_at_utc": utc_now(),
                "path": path,
                "params_without_key": params,
                "response": result,
            },
        )
        if str(result.get("status")) != "1":
            raise AmapError(f"高德拒绝请求：{result.get('infocode')} / {result.get('info')}")
        return result

    def wgs84_to_gcj02(self, points):
        if not 1 <= len(points) <= 40:
            raise ValueError("官方坐标转换单次支持 1–40 个点。")
        result = self.request(
            "/v3/assistant/coordinate/convert",
            {
                "locations": "|".join(f"{lon:.6f},{lat:.6f}" for lon, lat in points),
                "coordsys": "gps",
                "output": "json",
            },
        )
        try:
            converted = [
                tuple(map(float, value.split(","))) for value in result["locations"].split(";")
            ]
            if len(converted) != len(points) or any(len(point) != 2 for point in converted):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise AmapError("官方坐标转换返回了不完整的点列表。") from None
        return converted

    def polygon_sample(self, ring_gcj02, *, types, max_pages=2, page_size=25):
        if not re.fullmatch(r"\d{6}(\|\d{6})*", types):
            raise ValueError("types 必须为明确的六位 POI 分类码，多个用 | 分隔。")
        if not 1 <= page_size <= 25 or not 1 <= max_pages <= 8:
            raise ValueError("page_size 为 1–25；本采样器最多请求 8 页且最多接收 200 条。")
        if len(ring_gcj02) < 4 or ring_gcj02[0] != ring_gcj02[-1]:
            raise ValueError("Query polygon must be a closed GCJ-02 ring.")
        polygon = "|".join(f"{lon:.6f},{lat:.6f}" for lon, lat in ring_gcj02)
        seen, records, raw_count = set(), [], 0
        stop = "max_pages"
        for page in range(1, max_pages + 1):
            if self.calls >= self.max_requests:
                stop = "request_budget"
                break
            response = self.request(
                "/v5/place/polygon",
                {
                    "polygon": polygon,
                    "types": types,
                    "page_size": str(page_size),
                    "page_num": str(page),
                    "show_fields": "business,navi,children",
                },
            )
            pois = response.get("pois", [])
            if not isinstance(pois, list):
                raise AmapError("Unexpected POI response schema.")
            raw_count += len(pois)
            new = 0
            for poi in pois:
                uid = poi.get("id")
                if not uid or uid in seen:
                    continue
                seen.add(uid)
                records.append(poi)
                new += 1
            if not pois or len(pois) < page_size:
                stop = "short_page"
                break
            if new == 0:
                stop = "repeated_page"
                break
            if raw_count >= 200:
                stop = "api_200_record_cap"
                break
        return records, {
            "stop_reason": stop,
            "unique_pois": len(records),
            "raw_pois": raw_count,
            "requests_including_coordinate_conversion": self.calls,
            "sample_only": True,
            "completeness_not_guaranteed": True,
            "possible_truncation": stop != "short_page",
            "coordinate_reference": "GCJ-02 (Amap); NOT EPSG:4326",
            "same_query_official_limit": 200,
        }
