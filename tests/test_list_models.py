"""Tests for selecting all models when none are named."""

from __future__ import annotations

from pathlib import Path

from omove.config import Settings
from omove.store import list_model_names
from tests.helpers import write_blob, write_manifest


def test_list_model_names(stores: tuple[Path, Path, Settings]) -> None:
    hot, _cold, settings = stores
    a = write_blob(hot, b"a-data")
    b = write_blob(hot, b"b-data")
    write_manifest(
        hot,
        "registry.ollama.ai/library/alpha/latest",
        config_digest=a,
        layer_digests=[],
        config_size=len(b"a-data"),
        layer_sizes=[],
    )
    write_manifest(
        hot,
        "registry.ollama.ai/library/beta/latest",
        config_digest=b,
        layer_digests=[],
        config_size=len(b"b-data"),
        layer_sizes=[],
    )
    names = list_model_names(hot, settings)
    assert names == ["alpha:latest", "beta:latest"]
