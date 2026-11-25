"""Download repository release data, verify checksums and safely restore missing files."""

import argparse
import hashlib
import json
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

import requests


def sha256(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def extract_missing(archive, destination):
    destination = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            relative = PurePosixPath(member.filename)
            if relative.is_absolute() or ".." in relative.parts or "\\" in member.filename:
                raise ValueError("Unsafe archive path.")
            if stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError("Archive symlinks are not allowed.")
            target = destination.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(destination):
                raise ValueError("Archive path escapes the destination.")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            # Existing research outputs are preserved; use a fresh destination for full restoration.
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            pending = target.with_name(target.name + ".restoring")
            with bundle.open(member) as source, pending.open("wb") as output:
                shutil.copyfileobj(source, output)
            pending.replace(target)


def main(args):
    document = json.loads(args.manifest.read_text(encoding="utf-8"))
    destination = args.destination.resolve()
    cache = destination / ".local/research-data-downloads"
    cache.mkdir(parents=True, exist_ok=True)
    for asset in document["assets"]:
        if args.part != "all" and asset["part"] != args.part:
            continue
        archive = cache / asset["name"]
        if not archive.exists():
            pending = archive.with_suffix(".downloading")
            print("Downloading", asset["name"], flush=True)
            with requests.get(asset["url"], stream=True, timeout=(15, 120)) as response:
                response.raise_for_status()
                with pending.open("wb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        output.write(chunk)
            pending.replace(archive)
        if archive.stat().st_size != asset["bytes"] or sha256(archive) != asset["sha256"]:
            raise ValueError(f"Checksum mismatch; extraction refused: {asset['name']}")
        extract_missing(archive, destination)
        print("Verified and restored missing files:", asset["name"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("datasets/suzhou-20261125.json"))
    parser.add_argument("--destination", type=Path, default=Path("."))
    parser.add_argument("--part", choices=["all", "sources", "analysis"], default="all")
    main(parser.parse_args())
