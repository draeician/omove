"""Tests for access checks without ownership changes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from omove.config import Settings
from omove.errors import StoreUnsafeError
from omove.system import ensure_cold_store, ensure_hot_store


def _settings(tmp_path: Path) -> Settings:
    hot = tmp_path / "hot"
    cold = tmp_path / "cold"
    (hot / "manifests").mkdir(parents=True)
    (hot / "blobs").mkdir(parents=True)
    return Settings(
        hot_root=hot.resolve(),
        cold_root=cold.resolve(),
        cold_mount=tmp_path.resolve(),
        export_root=(tmp_path / "exports").resolve(),
        ollama_user="nobody",
        ollama_service="ollama.service",
        lock_file=tmp_path / "lock",
        allow_unmounted_cold=True,
        allow_live_ollama=True,
    )


def test_ensure_cold_store_creates_dirs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with patch("omove.system.validate_cold_mount", lambda _s: None):
        ensure_cold_store(settings)
    assert (settings.cold_root / "manifests").is_dir()
    assert (settings.cold_root / "blobs").is_dir()


def test_ensure_hot_store_reports_unreadable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with patch("os.access", return_value=False):
        with pytest.raises(StoreUnsafeError, match="Cannot read"):
            ensure_hot_store(settings)


def test_ensure_cold_store_reports_unwritable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with patch("omove.system.validate_cold_mount", lambda _s: None):
        with patch("os.access", return_value=False):
            with pytest.raises(StoreUnsafeError, match="Cannot read/write"):
                ensure_cold_store(settings, writable=True)
