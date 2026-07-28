"""Tests for manifest loading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omove.errors import ManifestError
from omove.manifest import load_manifest


def test_load_manifest_ok(tmp_path: Path) -> None:
    digest = "sha256:" + ("a" * 64)
    path = tmp_path / "manifest"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "config": {"digest": digest, "size": 10},
                "layers": [{"digest": digest, "size": 20}],
            }
        ),
        encoding="utf-8",
    )
    info = load_manifest(path)
    assert info.digests == (digest,)
    assert info.logical_size == 30


def test_load_manifest_invalid_schema(tmp_path: Path) -> None:
    path = tmp_path / "manifest"
    path.write_text(json.dumps({"schemaVersion": 1}), encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(path)
