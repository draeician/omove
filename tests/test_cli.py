"""CLI smoke tests."""

from __future__ import annotations

from omove import __version__
from omove.cli import main


def test_version() -> None:
    assert main(["version"]) == 0


def test_help() -> None:
    assert main(["help"]) == 0


def test_version_flag(capsys) -> None:
    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capsys.readouterr().out
    assert __version__ in out
