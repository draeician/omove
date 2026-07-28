"""Test helpers for synthetic Ollama stores."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from omove.manifest import blob_filename


def digest_for(data: bytes) -> str:
    """Return sha256:hex digest for data."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def write_blob(root: Path, data: bytes) -> str:
    """Write a blob and return its digest."""
    digest = digest_for(data)
    blobs = root / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    path = blobs / blob_filename(digest)
    path.write_bytes(data)
    return digest


def write_manifest(
    root: Path,
    rel: str,
    *,
    config_digest: str,
    layer_digests: list[str],
    config_size: int,
    layer_sizes: list[int],
) -> Path:
    """Write a schemaVersion 2 manifest at manifests/rel."""
    path = root / "manifests" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": 2,
        "config": {"digest": config_digest, "size": config_size},
        "layers": [
            {"digest": digest, "size": size}
            for digest, size in zip(layer_digests, layer_sizes, strict=True)
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
