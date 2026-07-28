"""Ollama manifest loading and validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from omove.errors import ManifestError

_DIGEST_RE = re.compile(r"^sha256:[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class ManifestInfo:
    """Parsed Ollama manifest digests and logical size."""

    path: Path
    digests: tuple[str, ...]
    logical_size: int


def blob_filename(digest: str) -> str:
    """Convert sha256:hex digest to on-disk blob filename."""
    return f"sha256-{digest.removeprefix('sha256:')}"


def load_manifest(path: Path) -> ManifestInfo:
    """Load and validate an Ollama schemaVersion 2 manifest."""
    if not path.is_file() or path.is_symlink():
        raise ManifestError(f"Manifest is not a regular file: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"Invalid Ollama manifest: {path}") from exc

    if not isinstance(data, dict):
        raise ManifestError(f"Invalid Ollama manifest: {path}")
    if data.get("schemaVersion") != 2:
        raise ManifestError(f"Invalid Ollama manifest: {path}")
    config = data.get("config")
    layers = data.get("layers")
    if not isinstance(config, dict) or not isinstance(config.get("digest"), str):
        raise ManifestError(f"Invalid Ollama manifest: {path}")
    if not isinstance(layers, list):
        raise ManifestError(f"Invalid Ollama manifest: {path}")

    raw_digests: list[str] = [config["digest"]]
    for layer in layers:
        if isinstance(layer, dict) and "digest" in layer:
            raw_digests.append(layer["digest"])

    digests: list[str] = []
    for digest in raw_digests:
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise ManifestError(f"Invalid blob digest in {path}: {digest}")
        digests.append(digest.lower())

    if not digests:
        raise ManifestError(f"Manifest contains no blob digests: {path}")

    unique = tuple(sorted(set(digests)))

    config_size = config.get("size", 0) or 0
    layer_sizes = sum(
        (layer.get("size", 0) or 0)
        for layer in layers
        if isinstance(layer, dict)
    )
    logical_size = config_size + layer_sizes
    if not isinstance(logical_size, int) or logical_size < 0:
        raise ManifestError(f"Manifest contains invalid size metadata: {path}")

    return ManifestInfo(path=path, digests=unique, logical_size=int(logical_size))


def digests_for_gc(path: Path) -> list[str]:
    """Extract digests for GC reference set (lighter validation)."""
    if not path.is_file() or path.is_symlink():
        raise ManifestError(
            f"Cannot garbage-collect while a manifest is invalid: {path}"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(
            f"Cannot garbage-collect while a manifest is invalid: {path}"
        ) from exc
    config = data.get("config") if isinstance(data, dict) else None
    layers = data.get("layers") if isinstance(data, dict) else None
    if not isinstance(config, dict) or not isinstance(config.get("digest"), str):
        raise ManifestError(
            f"Cannot garbage-collect while a manifest is invalid: {path}"
        )
    if not isinstance(layers, list):
        raise ManifestError(
            f"Cannot garbage-collect while a manifest is invalid: {path}"
        )

    result: list[str] = []
    candidates = [config["digest"]]
    candidates.extend(
        layer.get("digest") for layer in layers if isinstance(layer, dict)
    )
    for digest in candidates:
        if digest is None:
            continue
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise ManifestError(
                f"Cannot garbage-collect because {path} contains an "
                "invalid digest."
            )
        result.append(digest.lower())
    return result
