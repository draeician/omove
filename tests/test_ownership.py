"""Tests for access checks and optional confirmed permission fixes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from omove.config import Settings
from omove.errors import StoreUnsafeError
from omove.system import (
    ensure_cold_store,
    ensure_hot_store,
    ensure_store_access,
    iter_store_dirs,
    validate_chown_owner,
)


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


def test_ensure_hot_store_reports_unwritable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with patch("os.access", return_value=False):
        with pytest.raises(StoreUnsafeError, match="Cannot read/write"):
            ensure_hot_store(settings, writable=True)


def test_ensure_cold_store_reports_unwritable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with patch("omove.system.validate_cold_mount", lambda _s: None):
        with patch("os.access", return_value=False):
            with pytest.raises(StoreUnsafeError, match="Cannot read/write"):
                ensure_cold_store(settings, writable=True)


def test_iter_store_dirs_includes_nested_manifest_dirs(tmp_path: Path) -> None:
    root = tmp_path / "store"
    nested = root / "manifests" / "registry.ollama.ai" / "library" / "moondream"
    nested.mkdir(parents=True)
    (root / "blobs").mkdir(parents=True)
    paths = iter_store_dirs(root)
    assert root in paths
    assert root / "manifests" in paths
    assert root / "blobs" in paths
    assert nested in paths
    assert nested.parent in paths


def test_ensure_store_access_lists_all_failures(tmp_path: Path) -> None:
    root = tmp_path / "store"
    nested_a = root / "manifests" / "a"
    nested_b = root / "manifests" / "b"
    nested_a.mkdir(parents=True)
    nested_b.mkdir(parents=True)
    (root / "blobs").mkdir(parents=True)

    def fake_access(path: object, mode: int) -> bool:
        del mode
        text = str(path)
        return not text.endswith(("/a", "/b"))

    with patch("os.access", side_effect=fake_access):
        with pytest.raises(StoreUnsafeError) as excinfo:
            ensure_store_access("hot", root, writable=True, offer_fix=False)
    message = str(excinfo.value)
    assert str(nested_a) in message
    assert str(nested_b) in message
    assert "2 hot store path" in message


def test_ensure_store_access_chmod_fix_accepted(tmp_path: Path) -> None:
    root = tmp_path / "store"
    nested = root / "manifests" / "library" / "model"
    nested.mkdir(parents=True)
    (root / "blobs").mkdir(parents=True)
    denied = {nested}
    chmod = MagicMock()
    chown = MagicMock()

    def fake_access(path: object, mode: int) -> bool:
        del mode
        return Path(path) not in denied

    def fake_prompt(question: str) -> bool:
        assert "group-write" in question
        denied.clear()
        return True

    with patch("os.access", side_effect=fake_access):
        ensure_store_access(
            "cold",
            root,
            writable=True,
            offer_fix=True,
            owner="ollama",
            prompt=fake_prompt,
            chmod=chmod,
            chown=chown,
        )
    chmod.assert_called_once()
    assert nested in chmod.call_args.args[0]
    chown.assert_not_called()


def test_ensure_store_access_chown_after_chmod_insufficient(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    nested = root / "manifests" / "hf.co" / "model"
    nested.mkdir(parents=True)
    (root / "blobs").mkdir(parents=True)
    denied = {nested}
    chmod = MagicMock()
    chown = MagicMock()
    prompts: list[str] = []

    def fake_access(path: object, mode: int) -> bool:
        del mode
        return Path(path) not in denied

    def fake_prompt(question: str) -> bool:
        prompts.append(question)
        if "chown" in question:
            denied.clear()
            return True
        return True  # accept chmod first; access still denied until chown

    with patch("os.access", side_effect=fake_access):
        ensure_store_access(
            "cold",
            root,
            writable=True,
            offer_fix=True,
            owner="ollama",
            prompt=fake_prompt,
            chmod=chmod,
            chown=chown,
        )
    assert any("group-write" in q for q in prompts)
    assert any("chown" in q for q in prompts)
    chown.assert_called_once()
    assert nested in chown.call_args.args[0]
    assert chown.call_args.args[1] == "ollama"
    # chmod once before chown, once after
    assert chmod.call_count == 2


def test_ensure_store_access_fix_declined(tmp_path: Path) -> None:
    root = tmp_path / "store"
    nested = root / "manifests" / "library"
    nested.mkdir(parents=True)
    (root / "blobs").mkdir(parents=True)

    with patch("os.access", return_value=False):
        with pytest.raises(StoreUnsafeError, match="declined"):
            ensure_store_access(
                "cold",
                root,
                writable=True,
                offer_fix=True,
                owner="ollama",
                prompt=lambda _q: False,
                chmod=MagicMock(),
                chown=MagicMock(),
            )


def test_validate_chown_owner_rejects_unsafe_names() -> None:
    with pytest.raises(StoreUnsafeError):
        validate_chown_owner("ollama:root")
    with pytest.raises(StoreUnsafeError):
        validate_chown_owner("../evil")
    with pytest.raises(StoreUnsafeError):
        validate_chown_owner("ollama user")
    assert validate_chown_owner("ollama") == "ollama"
