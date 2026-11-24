"""Resume a modest regional OSM PBF with bounded range requests and publisher MD5."""

import argparse
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from urban_fabric.cleaning import file_sha256
from urban_fabric.data import utc_now, write_json


def download(url, target, workers=4):
    if not 1 <= workers <= 6:
        raise ValueError("Use 1–6 workers for this public static file service.")
    checksum = requests.get(url + ".md5", timeout=30)
    checksum.raise_for_status()
    expected_md5 = checksum.text.split()[0]
    if target.exists():
        if hashlib.md5(target.read_bytes()).hexdigest() != expected_md5:
            raise ValueError("Existing file differs from the current publisher MD5; preserve it.")
        print("Existing verified PBF preserved:", target)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=(20, 30)) as probe:
        if probe.status_code != 206 or "Content-Range" not in probe.headers:
            raise ValueError("The source must support HTTP byte ranges.")
        total = int(probe.headers["Content-Range"].split("/")[1])
        etag = probe.headers.get("ETag")
    if total > 250 * 1024 * 1024:
        raise ValueError("This pilot downloader is limited to regional files below 250 MiB.")
    parts = target.parent / (target.stem + ".parts")
    parts.mkdir(exist_ok=True)
    manifest = parts / "source.json"
    expected = {"url": url, "size": total, "etag": etag, "publisher_md5": expected_md5}
    if manifest.exists():
        import json

        if json.loads(manifest.read_text()) != expected:
            raise ValueError("Source changed; keep old parts and use a new output path.")
    write_json(manifest, expected)
    chunk_size = 1024 * 1024
    count = (total + chunk_size - 1) // chunk_size

    def fetch(index):
        start, end = index * chunk_size, min(total - 1, (index + 1) * chunk_size - 1)
        part = parts / f"{index:04d}.part"
        if part.exists() and part.stat().st_size == end - start + 1:
            return
        for attempt in range(4):
            try:
                headers = {"Range": f"bytes={start}-{end}"}
                if etag:
                    headers["If-Range"] = etag
                with requests.get(url, headers=headers, stream=True, timeout=(20, 40)) as response:
                    if response.status_code != 206 or response.headers.get("Content-Range") != (
                        f"bytes {start}-{end}/{total}"
                    ):
                        raise ValueError("Unexpected byte-range response or changed source.")
                    temporary = part.with_suffix(".tmp")
                    with temporary.open("wb") as handle:
                        for block in response.iter_content(65536):
                            handle.write(block)
                    if temporary.stat().st_size != end - start + 1:
                        raise ValueError("Truncated byte range.")
                    temporary.replace(part)
                return
            except (requests.RequestException, OSError, ValueError):
                if attempt == 3:
                    raise
                time.sleep(2)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, index) for index in range(count)]
        for done, future in enumerate(as_completed(futures), 1):
            future.result()
            if done % 6 == 0 or done == count:
                print(f"Verified byte ranges: {done}/{count}", flush=True)
    temporary = target.with_suffix(".assembling")
    with temporary.open("wb") as handle:
        for index in range(count):
            handle.write((parts / f"{index:04d}.part").read_bytes())
    if hashlib.md5(temporary.read_bytes()).hexdigest() != expected_md5:
        raise ValueError("Publisher MD5 mismatch; the assembled file is quarantined.")
    temporary.replace(target)
    write_json(
        target.with_suffix(".acquisition.json"),
        {
            **expected,
            "acquired_at_utc": utc_now(),
            "sha256": file_sha256(target),
            "publisher_md5_passed": True,
        },
    )
    print("Complete and verified:", target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    download(args.url, args.output, args.workers)
