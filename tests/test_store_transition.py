"""Integration tests for list/verify/freeze/thaw on synthetic stores."""

from __future__ import annotations

import json
from pathlib import Path

from omove.config import Settings
from omove.store import build_list_rows, list_store, verify_store
from omove.system import Session, prepare_mutation
from omove.transition import transition_models


def test_list_ok_status(sample_model: dict[str, object]) -> None:
    settings = sample_model["settings"]
    assert isinstance(settings, Settings)
    rows = build_list_rows(settings.hot_root, settings)
    assert len(rows) == 1
    assert rows[0].status == "OK"
    assert rows[0].name == "tiny:latest"


def test_list_json(sample_model: dict[str, object], capsys) -> None:
    settings = sample_model["settings"]
    assert isinstance(settings, Settings)
    assert list_store(settings, "hot", as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["status"] == "OK"


def test_list_aligns_long_names(
    sample_model: dict[str, object],
    capsys,
) -> None:
    """NAME column must expand so ID/SIZE stay aligned."""
    from tests.helpers import write_blob, write_manifest

    settings = sample_model["settings"]
    assert isinstance(settings, Settings)
    hot = settings.hot_root
    long_rel = (
        "hf.co/mradermacher/"
        "Qwen3-30B-A3B-abliterated-erotic-i1-GGUF/Q4_K_M"
    )
    blob = write_blob(hot, b"long-name-layer")
    write_manifest(
        hot,
        long_rel,
        config_digest=blob,
        layer_digests=[blob],
        config_size=15,
        layer_sizes=[15],
    )
    assert list_store(settings, "hot", as_json=False) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    header, sep, *rows = lines
    assert set(sep) <= {"-"}
    assert len(sep) == len(header)
    id_at = header.index("ID")
    size_at = header.index("SIZE")
    for row in rows:
        assert len(row) >= size_at
        id_token = row[id_at:size_at].strip()
        assert id_token == "-" or (
            len(id_token) == 12
            and all(c in "0123456789abcdef" for c in id_token)
        )
        # Long name must not spill into the ID column.
        assert row[id_at - 2 : id_at] == "  "


def test_verify_hot(sample_model: dict[str, object]) -> None:
    settings = sample_model["settings"]
    assert isinstance(settings, Settings)
    assert verify_store(settings, "hot", ["tiny"]) == 0


def test_freeze_thaw_roundtrip(sample_model: dict[str, object]) -> None:
    settings = sample_model["settings"]
    assert isinstance(settings, Settings)
    session = prepare_mutation(settings, skip_privileges=True, stop_service=False)
    with session:
        assert (
            transition_models(session, "freeze", ["tiny"], dry_run=False) == 0
        )
    hot_manifest = settings.hot_root / "manifests" / str(sample_model["rel"])
    assert not hot_manifest.exists()
    cold_manifest = (
        settings.cold_root / "manifests" / str(sample_model["rel"])
    )
    assert cold_manifest.is_file()

    session2 = prepare_mutation(
        settings, skip_privileges=True, stop_service=False
    )
    with session2:
        assert transition_models(session2, "thaw", ["tiny"]) == 0
    assert hot_manifest.is_file()
    assert not cold_manifest.exists()


def test_freeze_dry_run_no_mutation(sample_model: dict[str, object]) -> None:
    settings = sample_model["settings"]
    assert isinstance(settings, Settings)
    session = prepare_mutation(settings, skip_privileges=True, stop_service=False)
    with session:
        assert (
            transition_models(session, "freeze", ["tiny"], dry_run=True) == 0
        )
    assert (
        settings.hot_root / "manifests" / str(sample_model["rel"])
    ).is_file()
    assert not any(
        (settings.cold_root / "manifests").rglob("*")
        if (settings.cold_root / "manifests").exists()
        else []
    )
