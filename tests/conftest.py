"""Shared fixtures for synthetic Ollama stores."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omove.config import Settings
from tests.helpers import write_blob, write_manifest


@pytest.fixture
def stores(tmp_path: Path) -> tuple[Path, Path, Settings]:
    """Create hot/cold store roots and matching Settings."""
    hot = tmp_path / "hot"
    cold = tmp_path / "cold"
    for root in (hot, cold):
        (root / "manifests").mkdir(parents=True)
        (root / "blobs").mkdir(parents=True)

    settings = Settings(
        hot_root=hot.resolve(),
        cold_root=cold.resolve(),
        cold_mount=cold.resolve().parent,
        ollama_user=os.environ.get("USER", "nobody"),
        ollama_service="ollama.service",
        lock_file=tmp_path / "omove.lock",
        allow_unmounted_cold=True,
        allow_live_ollama=True,
    )
    return hot, cold, settings


@pytest.fixture
def sample_model(stores: tuple[Path, Path, Settings]) -> dict[str, object]:
    """Place a small complete model in hot storage."""
    hot, _cold, settings = stores
    config_data = b"config-bytes"
    layer_data = b"layer-bytes-001"
    config_digest = write_blob(hot, config_data)
    layer_digest = write_blob(hot, layer_data)
    rel = "registry.ollama.ai/library/tiny/latest"
    write_manifest(
        hot,
        rel,
        config_digest=config_digest,
        layer_digests=[layer_digest],
        config_size=len(config_data),
        layer_sizes=[len(layer_data)],
    )
    return {
        "settings": settings,
        "hot": hot,
        "rel": rel,
        "config_digest": config_digest,
        "layer_digest": layer_digest,
    }
