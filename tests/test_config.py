"""Tests for config file loading."""

from __future__ import annotations

from pathlib import Path

from omove.config import Settings, write_config_template


def test_config_file_overrides_defaults(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "\n".join(
            [
                'hot_path = "/tmp/omove-hot"',
                'cold_path = "/tmp/omove-cold/archive"',
                'cold_mount = "/tmp/omove-cold"',
                "allow_unmounted_cold = true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OMOVE_HOT_PATH", raising=False)
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    monkeypatch.delenv("OMOVE_COLD_PATH", raising=False)
    monkeypatch.delenv("OMOVE_COLD_MOUNT", raising=False)
    monkeypatch.delenv("OMOVE_ALLOW_UNMOUNTED_COLD", raising=False)

    settings = Settings.load(config_path=cfg)
    assert settings.hot_root == Path("/tmp/omove-hot").resolve()
    assert settings.cold_root == Path("/tmp/omove-cold/archive").resolve()
    assert settings.cold_mount == Path("/tmp/omove-cold").resolve()
    assert settings.allow_unmounted_cold is True


def test_env_overrides_config(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('cold_path = "/tmp/from-file"\n', encoding="utf-8")
    monkeypatch.setenv("OMOVE_COLD_PATH", "/tmp/from-env")
    settings = Settings.load(config_path=cfg)
    assert settings.cold_root == Path("/tmp/from-env").resolve()


def test_config_init(tmp_path: Path) -> None:
    path = write_config_template(tmp_path / "config.toml")
    assert path.is_file()
    assert "cold_path" in path.read_text(encoding="utf-8")
