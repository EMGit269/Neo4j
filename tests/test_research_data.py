"""Release restoration must preserve local work and reject corrupt or unsafe archives."""

import hashlib
import importlib.util
import json
import stat
import zipfile
from argparse import Namespace
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "download_research_data",
    Path(__file__).resolve().parents[1] / "scripts/download_research_data.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_restore_missing_files_preserves_existing_research(tmp_path):
    archive = tmp_path / "research.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("data/existing.txt", "published result")
        bundle.writestr("source_files/苏州模型.txt", "原始模型")
    destination = tmp_path / "restore"
    (destination / "data").mkdir(parents=True)
    existing = destination / "data/existing.txt"
    existing.write_text("new local result")
    module.extract_missing(archive, destination)
    assert existing.read_text() == "new local result"
    assert (destination / "source_files/苏州模型.txt").read_text() == "原始模型"


@pytest.mark.parametrize("name", ["../outside.txt", "/outside.txt", "data/../../outside.txt"])
def test_archive_path_cannot_escape_destination(tmp_path, name):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(name, "unwanted write")
    with pytest.raises(ValueError, match="Unsafe archive path"):
        module.extract_missing(archive, tmp_path / "restore")
    assert not (tmp_path / "outside.txt").exists()


def test_archive_symlink_is_rejected(tmp_path):
    archive = tmp_path / "unsafe.zip"
    entry = zipfile.ZipInfo("data/link")
    entry.create_system = 3
    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(entry, "../../outside")
    with pytest.raises(ValueError, match="symlinks"):
        module.extract_missing(archive, tmp_path / "restore")


@pytest.mark.parametrize("corruption", ["size", "digest"])
def test_corrupt_cached_asset_is_rejected_before_extraction(tmp_path, monkeypatch, corruption):
    cache = tmp_path / ".local/research-data-downloads"
    cache.mkdir(parents=True)
    archive = cache / "research.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("data/result.txt", "published result")
    asset = {
        "part": "analysis",
        "name": archive.name,
        "bytes": archive.stat().st_size,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    if corruption == "size":
        asset["bytes"] += 1
    else:
        asset["sha256"] = "0" * 64
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"assets": [asset]}))
    monkeypatch.setattr(
        module.requests, "get", lambda *a, **k: pytest.fail("Cached file triggered a download")
    )
    with pytest.raises(ValueError, match="Checksum mismatch"):
        module.main(Namespace(manifest=manifest, destination=tmp_path, part="analysis"))
    assert not (tmp_path / "data/result.txt").exists()
