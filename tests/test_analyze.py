"""Tests for blob sharing analysis."""

from __future__ import annotations

import json
from pathlib import Path

from omove.analyze import analyze_store, build_store_index
from omove.config import Settings
from tests.helpers import write_blob, write_manifest


def test_analyze_shared_vs_unique(
    stores: tuple[Path, Path, Settings], capsys
) -> None:
    hot, _cold, settings = stores
    shared = write_blob(hot, b"shared-weights-xxxx")
    only_a = write_blob(hot, b"only-model-a")
    only_b = write_blob(hot, b"only-model-b")

    write_manifest(
        hot,
        "registry.ollama.ai/library/model-a/latest",
        config_digest=only_a,
        layer_digests=[shared],
        config_size=len(b"only-model-a"),
        layer_sizes=[len(b"shared-weights-xxxx")],
    )
    write_manifest(
        hot,
        "registry.ollama.ai/library/model-b/latest",
        config_digest=only_b,
        layer_digests=[shared],
        config_size=len(b"only-model-b"),
        layer_sizes=[len(b"shared-weights-xxxx")],
    )

    index = build_store_index(hot, "hot", settings)
    assert len(index.models) == 2
    assert len(index.digest_to_models[shared]) == 2

    rc = analyze_store(settings, "hot", ["model-a"], as_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    report = payload["models"][0]
    assert report["name"] == "model-a:latest"
    assert report["unique_bytes"] == len(b"only-model-a")
    assert report["shared_bytes"] == len(b"shared-weights-xxxx")
    statuses = {b["short"]: b["status"] for b in report["blobs"]}
    assert "UNIQUE" in statuses.values()
    assert "SHARED" in statuses.values()
    assert payload["summary"]["unique_bytes"] == len(b"only-model-a")
