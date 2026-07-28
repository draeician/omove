"""Tests for privilege model, locking, and systemctl sudo wrapping."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from omove.cli import main
from omove.system import (
    acquire_lock,
    open_lock_file,
    stop_ollama_for_mutation,
    systemctl_argv,
)


def test_systemctl_argv_uses_sudo_when_not_root() -> None:
    with (
        patch("omove.system.os.geteuid", return_value=1000),
        patch("omove.system.shutil.which", side_effect=lambda n: f"/usr/bin/{n}"),
    ):
        argv = systemctl_argv("is-active", "ollama.service")
    assert argv == [
        "/usr/bin/sudo",
        "/usr/bin/systemctl",
        "is-active",
        "ollama.service",
    ]


def test_systemctl_argv_bare_when_root() -> None:
    with (
        patch("omove.system.os.geteuid", return_value=0),
        patch("omove.system.shutil.which", side_effect=lambda n: f"/usr/bin/{n}"),
    ):
        argv = systemctl_argv("stop", "ollama.service")
    assert argv == ["/usr/bin/systemctl", "stop", "ollama.service"]


def test_stop_ollama_calls_is_active_without_quiet() -> None:
    calls: list[tuple[str, ...]] = []

    def fake_ctl(*args: str):
        calls.append(args)

        class R:
            returncode = 1

        return R()

    class Settings:
        ollama_service = "ollama.service"
        allow_live_ollama = True

    with patch("omove.system.shutil.which", return_value="/usr/bin/systemctl"):
        stop_ollama_for_mutation(
            Settings(),  # type: ignore[arg-type]
            systemctl=fake_ctl,
            pgrep=lambda *a: type("R", (), {"returncode": 1})(),
        )
    assert calls[0] == ("is-active", "ollama.service")


def test_acquire_lock_interrupt(tmp_path: Path) -> None:
    lock = tmp_path / "omove.lock"

    def raise_interrupt(_fd: int, _flags: int) -> None:
        raise KeyboardInterrupt

    with patch("omove.system.fcntl.flock", side_effect=raise_interrupt):
        with pytest.raises(KeyboardInterrupt):
            acquire_lock(lock)


def test_open_lock_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    preferred = Path("/proc/omove-unwritable-lock-dir/omove.lock")
    runtime = tmp_path / "run"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    path, fd = open_lock_file(preferred)
    try:
        assert path == runtime / "omove.lock"
    finally:
        os.close(fd)


def test_main_keyboard_interrupt() -> None:
    with patch("omove.cli.Settings.load", side_effect=KeyboardInterrupt):
        assert main(["list", "hot"]) == 130
