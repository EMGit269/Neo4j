"""Download immutable, checksum-verified official GBA tiles with resumable ranges."""

import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml
from shapely.geometry import shape

from .data import utc_now, write_json
from .gba import district_boundary, process_district


def load_gba_config(path):
    path = Path(path).resolve()
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    area, paths, gba = doc["study_area"], doc["paths"], doc["gba"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", area["id"]):
        raise ValueError("Invalid study_area.id")
    tile = gba["tile"]
    if not re.fullmatch(r"[a-z]+/[ew]\d+_[ns]\d+_[ew]\d+_[ns]\d+", tile):
        raise ValueError("Invalid GBA tile path")
    workers, chunk_mb = int(gba.get("workers", 8)), int(gba.get("chunk_size_mb", 32))
    if not (1 <= workers <= 16 and 1 <= chunk_mb <= 128):
        raise ValueError("workers must be 1–16; chunk_size_mb must be 1–128")
    polygon_parts = gba.get("polygon_parts", ["odbl", "polygon"])
    if not polygon_parts or set(polygon_parts) - {"odbl", "polygon"}:
        raise ValueError("polygon_parts must contain odbl and/or polygon")
    selection_bbox = area.get("selection_bbox")
    if selection_bbox:
        if len(selection_bbox) != 4:
            raise ValueError("selection_bbox requires [west,south,east,north]")
        west, south, east, north = selection_bbox
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError("Invalid selection_bbox")
    return {
        "id": area["id"],
        "name": area["name"],
        "division_code": str(area["division_code"]),
        "boundary_file": (path.parent / area["boundary_file"]).resolve(),
        "overpass_query": area["overpass_query"],
        "data_dir": (path.parent / paths["data_dir"]).resolve(),
        "cache_dir": (path.parent / paths["cache_dir"]).resolve(),
        "tile": tile,
        "metric_crs": gba.get("metric_crs", "EPSG:32650"),
        "host": gba.get("download_host", "https://huggingface.co").rstrip("/"),
        "workers": workers,
        "chunk_size": chunk_mb * 1024 * 1024,
        "polygon_parts": polygon_parts,
        "selection_bbox": selection_bbox,
    }


def _request(url, *, data=None, headers=None, timeout=90):
    request = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": "UrbanFabricLab/0.1 academic research", **(headers or {})},
    )
    return urllib.request.urlopen(request, timeout=timeout)


def prepare_downloads(config):
    source = config["data_dir"] / "source"
    source.mkdir(parents=True, exist_ok=True)
    config["cache_dir"].mkdir(parents=True, exist_ok=True)
    boundary_file = config["boundary_file"]
    if not boundary_file.exists():
        payload = urllib.parse.urlencode({"data": config["overpass_query"]}).encode()
        with _request("https://overpass-api.de/api/interpreter", data=payload, timeout=180) as r:
            document = json.load(r)
        district_boundary(document, config["division_code"])
        write_json(boundary_file, document)
    document = json.loads(boundary_file.read_text(encoding="utf-8"))
    _, boundary = district_boundary(document, config["division_code"])
    index_file = source / "lod1_index.geojson"
    if not index_file.exists():
        url = (
            config["host"] + "/datasets/zhu-xlab/GBA.LoD1/resolve/main/representative/lod1.geojson"
        )
        with _request(url) as r:
            index_file.write_bytes(r.read())
    index = json.loads(index_file.read_text(encoding="utf-8"))
    tile_feature = [f for f in index["features"] if f["properties"]["tile"] == config["tile"]]
    if len(tile_feature) != 1 or not shape(tile_feature[0]["geometry"]).covers(boundary):
        raise ValueError("配置的单个 GBA 分块未完整覆盖区界，请显式配置覆盖全区的分块。")
    records = []
    for kind, repo, folder, suffix in [
        ("polygon", "zhu-xlab/GBA.LoD1", "Polygon/", ".geojson"),
        ("height", "zhu-xlab/GBA.LoD1", "LoD1/", ".json"),
        ("odbl", "zhu-xlab/GBA.ODbLPolygon", "", ".geojson"),
    ]:
        if kind != "height" and kind not in config["polygon_parts"]:
            continue
        relative = folder + config["tile"] + suffix
        listing_file = source / (kind + "_file_listing.json")
        if listing_file.exists():
            listing = json.loads(listing_file.read_text(encoding="utf-8"))
        else:
            parent = str(Path(relative).parent)
            url = config["host"] + f"/api/datasets/{repo}/tree/main/{parent}?limit=1000"
            with _request(url) as r:
                listing = json.load(r)
            listing = [row for row in listing if row["path"] == relative]
            write_json(listing_file, listing)
        selected = [row for row in listing if row.get("path") == relative]
        if len(selected) != 1 or not selected[0].get("lfs", {}).get("oid"):
            raise ValueError("Official GBA file metadata/SHA256 not found: " + relative)
        row = selected[0].copy()
        row.update(
            kind=kind,
            url=config["host"] + f"/datasets/{repo}/resolve/main/{relative}",
            canonical_url=f"https://huggingface.co/datasets/{repo}/resolve/main/{relative}",
            local_path=str(config["cache_dir"] / (kind + "_" + Path(relative).name)),
        )
        records.append(row)
    write_json(source / "download_plan.json", records)
    return records


def download_tiles(records, *, workers=8, chunk_size=32 * 1024 * 1024, progress=print):
    jobs, states = [], {}
    completed = 0
    total = sum(row["size"] for row in records)
    for row in records:
        destination = Path(row["local_path"])
        marker = destination.with_suffix(destination.suffix + ".ranges.json")
        if destination.exists() and not marker.exists():
            completed += row["size"]
            continue
        state = (
            json.loads(marker.read_text())
            if marker.exists()
            else {
                "sha256": row["lfs"]["oid"],
                "size": row["size"],
                "chunk_size": chunk_size,
                "completed": [],
            }
        )
        if state["sha256"] != row["lfs"]["oid"] or state["size"] != row["size"]:
            raise ValueError("Partial download metadata differs from source; file preserved.")
        if state.get("chunk_size", 32 * 1024 * 1024) != chunk_size:
            raise ValueError("Partial download chunk size differs; preserve the original setting.")
        if not destination.exists():
            state["completed"] = []
            with destination.open("wb") as handle:
                handle.truncate(row["size"])
            write_json(marker, state)
        states[row["kind"]] = state, marker
        for lo in range(0, row["size"], chunk_size):
            hi = min(row["size"] - 1, lo + chunk_size - 1)
            if lo in state["completed"]:
                completed += hi - lo + 1
            else:
                jobs.append((row, lo, hi))

    def fetch(job):
        row, lo, hi = job
        for attempt in range(5):
            try:
                with _request(row["url"], headers={"Range": f"bytes={lo}-{hi}"}) as r:
                    expected = f"bytes {lo}-{hi}/{row['size']}"
                    if r.status != 206 or r.headers.get("Content-Range") != expected:
                        raise ValueError("Server range mismatch")
                    with Path(row["local_path"]).open("r+b", buffering=0) as handle:
                        handle.seek(lo)
                        remaining = hi - lo + 1
                        while remaining:
                            block = r.read(min(remaining, 1024 * 1024))
                            if not block:
                                raise OSError("Incomplete HTTP range")
                            handle.write(block)
                            remaining -= len(block)
                return row, lo, hi
            except (OSError, ValueError):
                if attempt == 4:
                    raise
                time.sleep(min(2**attempt, 10))

    start, last = time.monotonic(), 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, job) for job in jobs]
        for future in as_completed(futures):
            row, lo, hi = future.result()
            completed += hi - lo + 1
            state, marker = states[row["kind"]]
            state["completed"].append(lo)
            temporary = marker.with_suffix(".tmp")
            write_json(temporary, state)
            temporary.replace(marker)
            if time.monotonic() - last >= 20:
                progress(
                    f"GBA download: {completed / 1e9:.2f}/{total / 1e9:.2f} GB; "
                    f"elapsed {time.monotonic() - start:.0f}s"
                )
                last = time.monotonic()
    for row in records:
        with Path(row["local_path"]).open("rb") as handle:
            checksum = hashlib.file_digest(handle, "sha256").hexdigest()
        if checksum != row["lfs"]["oid"]:
            raise ValueError("SHA256 mismatch; cached file preserved: " + row["kind"])
        row["verified_sha256"] = checksum
        row["verified_at_utc"] = utc_now()
        Path(row["local_path"] + ".ranges.json").unlink(missing_ok=True)
        progress("SHA256 verified: " + row["kind"])
    return records


def fetch_gba(path, *, process_only=False, progress=print):
    config = load_gba_config(path)
    if process_only:
        manifest = config["data_dir"] / "source/verified_downloads.json"
        records = json.loads(manifest.read_text(encoding="utf-8"))
        # Recompute checksums, rather than trusting a mutable file path alone.
        records = download_tiles(records, progress=progress)
    else:
        records = download_tiles(
            prepare_downloads(config),
            workers=config["workers"],
            chunk_size=config["chunk_size"],
            progress=progress,
        )
    write_json(config["data_dir"] / "source/verified_downloads.json", records)
    return process_district(config, records, progress)
