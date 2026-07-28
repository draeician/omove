"""Tests for global --silent / progress gating."""

from __future__ import annotations

from omove.transfer import (
    is_silent,
    progress_enabled,
    set_silent,
)


def test_progress_enabled_by_default() -> None:
    set_silent(False)
    assert progress_enabled() is True
    assert progress_enabled(True) is True
    assert progress_enabled(False) is False


def test_silent_disables_progress() -> None:
    set_silent(True)
    try:
        assert is_silent() is True
        assert progress_enabled() is False
        assert progress_enabled(True) is False
    finally:
        set_silent(False)


def test_cli_silent_flag_sets_global() -> None:
    from unittest.mock import patch

    from omove.cli import main

    set_silent(False)
    with patch("omove.cli.Settings.load", side_effect=KeyboardInterrupt):
        assert main(["--silent", "list", "hot"]) == 130
    assert is_silent() is True
    set_silent(False)
