"""Tests for cold-mount safety checks."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from omove.config import Settings
from omove.errors import StoreUnsafeError
from omove.system import validate_cold_mount


def _settings(tmp_path: Path, *, allow: bool = False) -> Settings:
    cold = tmp_path / "archive"
    cold.mkdir()
    return Settings(
        hot_root=(tmp_path / "hot").resolve(),
        cold_root=cold.resolve(),
        cold_mount=tmp_path.resolve(),
        export_root=(tmp_path / "exports").resolve(),
        ollama_user="nobody",
        ollama_service="ollama.service",
        lock_file=tmp_path / "lock",
        allow_unmounted_cold=allow,
        allow_live_ollama=True,
    )


def test_nested_archive_ok_when_not_on_root(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with patch("omove.system.filesystem_mount", return_value=Path("/opt/md2")):
        validate_cold_mount(settings)


def test_root_filesystem_refused(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with patch("omove.system.filesystem_mount", return_value=Path("/")):
        with pytest.raises(StoreUnsafeError, match="root filesystem"):
            validate_cold_mount(settings)


def test_root_allowed_when_override(tmp_path: Path) -> None:
    settings = _settings(tmp_path, allow=True)
    with patch("omove.system.filesystem_mount", return_value=Path("/")):
        validate_cold_mount(settings)
