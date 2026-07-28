"""Configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_HOST = "registry.ollama.ai"
DEFAULT_NAMESPACE = "library"
DEFAULT_TAG = "latest"

_DEFAULT_HOT = "/usr/share/ollama/.ollama/models"
_DEFAULT_COLD = "/media/elysium/ollama_archive"
_DEFAULT_USER = "ollama"
_DEFAULT_SERVICE = "ollama.service"
_DEFAULT_LOCK = "/run/lock/omove.lock"


def _truthy(value: str | None) -> bool:
    return value == "1"


def _resolve(path: str) -> Path:
    return Path(path).resolve()


@dataclass(frozen=True)
class Settings:
    """Runtime settings for omove."""

    hot_root: Path
    cold_root: Path
    cold_mount: Path
    ollama_user: str
    ollama_service: str
    lock_file: Path
    allow_unmounted_cold: bool
    allow_live_ollama: bool
    default_host: str = DEFAULT_HOST
    default_namespace: str = DEFAULT_NAMESPACE
    default_tag: str = DEFAULT_TAG

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        """Build settings from environment overrides."""
        env = environ if environ is not None else dict(os.environ)
        hot = env.get("OMOVE_HOT_PATH") or env.get("OLLAMA_MODELS") or _DEFAULT_HOT
        cold = env.get("OMOVE_COLD_PATH") or _DEFAULT_COLD
        cold_root = _resolve(cold)
        cold_mount_raw = env.get("OMOVE_COLD_MOUNT") or str(cold_root.parent)
        return cls(
            hot_root=_resolve(hot),
            cold_root=cold_root,
            cold_mount=_resolve(cold_mount_raw),
            ollama_user=env.get("OMOVE_OLLAMA_USER") or _DEFAULT_USER,
            ollama_service=env.get("OMOVE_OLLAMA_SERVICE") or _DEFAULT_SERVICE,
            lock_file=_resolve(env.get("OMOVE_LOCK_FILE") or _DEFAULT_LOCK),
            allow_unmounted_cold=_truthy(env.get("OMOVE_ALLOW_UNMOUNTED_COLD")),
            allow_live_ollama=_truthy(env.get("OMOVE_ALLOW_LIVE_OLLAMA")),
        )

    def root_for(self, tier: str) -> Path:
        """Return hot or cold root for a tier name."""
        if tier == "hot":
            return self.hot_root
        if tier == "cold":
            return self.cold_root
        raise ValueError(f"Unknown tier: {tier}")


PRESERVE_ENV = (
    "OLLAMA_MODELS",
    "OMOVE_HOT_PATH",
    "OMOVE_COLD_PATH",
    "OMOVE_COLD_MOUNT",
    "OMOVE_OLLAMA_USER",
    "OMOVE_OLLAMA_SERVICE",
    "OMOVE_LOCK_FILE",
    "OMOVE_ALLOW_UNMOUNTED_COLD",
    "OMOVE_ALLOW_LIVE_OLLAMA",
)
