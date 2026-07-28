"""Tests for path canonicalization and query matching."""

from __future__ import annotations

from omove.paths import (
    LayoutKind,
    canonicalize_manifest_relpath,
    legacy_relpath_for_canonical,
    manifest_display_name,
    query_matches_relpath,
)


def test_canonicalize_canonical() -> None:
    result = canonicalize_manifest_relpath(
        "registry.ollama.ai/library/llama3.2/latest"
    )
    assert result is not None
    assert result.layout == LayoutKind.CANONICAL
    assert result.rel == "registry.ollama.ai/library/llama3.2/latest"


def test_canonicalize_legacy_cold() -> None:
    result = canonicalize_manifest_relpath("llama3.2/latest")
    assert result is not None
    assert result.layout == LayoutKind.LEGACY_COLD
    assert result.rel == "registry.ollama.ai/library/llama3.2/latest"


def test_canonicalize_legacy_flat() -> None:
    result = canonicalize_manifest_relpath(
        "registry.ollama.ai/library/llama3.2:latest"
    )
    assert result is not None
    assert result.layout == LayoutKind.LEGACY_FLAT
    assert result.rel == "registry.ollama.ai/library/llama3.2/latest"


def test_display_name_short() -> None:
    assert (
        manifest_display_name("registry.ollama.ai/library/llama3.2/latest")
        == "llama3.2:latest"
    )


def test_query_matches_short_and_tagless() -> None:
    rel = "registry.ollama.ai/library/llama3.2/latest"
    assert query_matches_relpath("llama3.2", rel)
    assert query_matches_relpath("llama3.2:latest", rel)
    assert query_matches_relpath("library/llama3.2:latest", rel)


def test_legacy_relpath_for_canonical() -> None:
    assert (
        legacy_relpath_for_canonical(
            "registry.ollama.ai/library/llama3.2/latest"
        )
        == "llama3.2/latest"
    )
    assert (
        legacy_relpath_for_canonical(
            "registry.example.com/team/model/prod"
        )
        is None
    )
