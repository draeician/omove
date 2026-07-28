"""Configuration from file + environment variables.

Precedence (highest wins): environment → config file → built-in defaults.

Config path: ``$XDG_CONFIG_HOME/omove/config.toml`` (default
``~/.config/omove/config.toml``). When re-executed under sudo, the
invoking user's config is used via ``SUDO_USER`` / preserved ``HOME``.
"""

from __future__ import annotations

import os
import pwd
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Python 3.10 requires the 'tomli' package to read config.toml. "
            "Upgrade to Python 3.11+ or: pip install tomli"
        ) from exc


DEFAULT_HOST = "registry.ollama.ai"
DEFAULT_NAMESPACE = "library"
DEFAULT_TAG = "latest"

_DEFAULT_HOT = "/usr/share/ollama/.ollama/models"
_DEFAULT_COLD = "/media/elysium/ollama_archive"
_DEFAULT_USER = "ollama"
_DEFAULT_SERVICE = "ollama.service"
_DEFAULT_LOCK = "/run/lock/omove.lock"

CONFIG_TEMPLATE = """\
# omove configuration (TOML)
# Environment variables (OMOVE_*) override these values when set.

# Where Ollama keeps active models (the "hot" store Ollama reads).
hot_path = "/usr/share/ollama/.ollama/models"

# Where omove stores frozen/archived models (the "cold" archive).
cold_path = "/path/to/ollama_archive"

# Optional: pin cold storage to a specific disk mount (e.g. "/opt/md2").
# If omitted, omove auto-detects the mount that contains cold_path.
# You do NOT need this to be the parent folder of cold_path.
# cold_mount = "/opt/md2"

# Allow the cold archive to live on the root filesystem (/).
# Only enable this if you understand the risk (can fill the system disk).
# allow_unmounted_cold = false

# Allow freeze/thaw/migrate while an ollama process is still running.
# Unsafe: Ollama may be reading/writing the same files.
# allow_live_ollama = false

# ollama_user = "ollama"
# ollama_service = "ollama.service"
# lock_file = "/run/lock/omove.lock"
"""


def _truthy(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _resolve(path: str) -> Path:
    return Path(path).expanduser().resolve()


def user_home() -> Path:
    """Return the real user's home, even when running under sudo."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and os.geteuid() == 0:
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass
    return Path.home()


def default_config_path() -> Path:
    """Return the XDG config path for omove."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        base = Path(xdg).expanduser()
    else:
        base = user_home() / ".config"
    return base / "omove" / "config.toml"


def load_config_file(path: Path | None = None) -> dict[str, object]:
    """Load TOML config; return empty dict if missing."""
    config_path = path if path is not None else default_config_path()
    if not config_path.is_file():
        return {}
    try:
        raw = config_path.read_bytes()
    except OSError:
        return {}
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Invalid config file {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Invalid config file {config_path}: root must be a table")
    return data


def write_config_template(path: Path | None = None, *, force: bool = False) -> Path:
    """Write a starter config.toml; refuse to overwrite unless force."""
    config_path = path if path is not None else default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.exists() and not force:
        raise FileExistsError(f"Config already exists: {config_path}")
    config_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return config_path


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
    config_path: Path | None = None

    @classmethod
    def load(
        cls,
        environ: dict[str, str] | None = None,
        config_path: Path | None = None,
    ) -> Settings:
        """Build settings: env overrides config file overrides defaults."""
        env = environ if environ is not None else dict(os.environ)
        file_cfg = load_config_file(config_path)
        resolved_path = (
            config_path if config_path is not None else default_config_path()
        )

        def file_str(*keys: str) -> str | None:
            for key in keys:
                value = file_cfg.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return None

        def file_bool(key: str) -> bool | None:
            if key not in file_cfg:
                return None
            return _truthy(file_cfg.get(key))  # type: ignore[arg-type]

        hot = (
            env.get("OMOVE_HOT_PATH")
            or env.get("OLLAMA_MODELS")
            or file_str("hot_path", "hot")
            or _DEFAULT_HOT
        )
        cold = (
            env.get("OMOVE_COLD_PATH")
            or file_str("cold_path", "cold")
            or _DEFAULT_COLD
        )
        cold_root = _resolve(cold)
        cold_mount_raw = (
            env.get("OMOVE_COLD_MOUNT")
            or file_str("cold_mount")
            or str(cold_root.parent)
        )

        allow_unmounted = env.get("OMOVE_ALLOW_UNMOUNTED_COLD")
        if allow_unmounted is not None:
            allow_unmounted_cold = _truthy(allow_unmounted)
        else:
            allow_unmounted_cold = file_bool("allow_unmounted_cold") or False

        allow_live = env.get("OMOVE_ALLOW_LIVE_OLLAMA")
        if allow_live is not None:
            allow_live_ollama = _truthy(allow_live)
        else:
            allow_live_ollama = file_bool("allow_live_ollama") or False

        return cls(
            hot_root=_resolve(hot),
            cold_root=cold_root,
            cold_mount=_resolve(cold_mount_raw),
            ollama_user=(
                env.get("OMOVE_OLLAMA_USER")
                or file_str("ollama_user")
                or _DEFAULT_USER
            ),
            ollama_service=(
                env.get("OMOVE_OLLAMA_SERVICE")
                or file_str("ollama_service")
                or _DEFAULT_SERVICE
            ),
            lock_file=_resolve(
                env.get("OMOVE_LOCK_FILE")
                or file_str("lock_file")
                or _DEFAULT_LOCK
            ),
            allow_unmounted_cold=allow_unmounted_cold,
            allow_live_ollama=allow_live_ollama,
            config_path=resolved_path if resolved_path.is_file() else None,
        )

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        """Compatibility alias for :meth:`load`."""
        return cls.load(environ=environ)

    def root_for(self, tier: str) -> Path:
        """Return hot or cold root for a tier name."""
        if tier == "hot":
            return self.hot_root
        if tier == "cold":
            return self.cold_root
        raise ValueError(f"Unknown tier: {tier}")


PRESERVE_ENV = (
    "HOME",
    "XDG_CONFIG_HOME",
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
