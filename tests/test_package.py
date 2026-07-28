"""Tests for export/import portable packages."""

from __future__ import annotations

from pathlib import Path

from omove.config import Settings
from omove.package import export_model, import_package
from omove.system import prepare_mutation
from omove.store import resolve_manifest


def test_export_import_roundtrip(sample_model: dict[str, object]) -> None:
    settings = sample_model["settings"]
    assert isinstance(settings, Settings)
    export_dir = settings.export_root
    export_dir.mkdir(parents=True, exist_ok=True)

    session = prepare_mutation(settings, skip_privileges=True, stop_service=False)
    with session:
        package = export_model(
            session, "tiny", source_tier="hot", output=export_dir
        )
    assert package.is_file()
    assert package.name.endswith(".omove.tar.gz")

    # Remove from hot so import has a clean target.
    rel = str(sample_model["rel"])
    (settings.hot_root / "manifests" / rel).unlink()

    session2 = prepare_mutation(
        settings, skip_privileges=True, stop_service=False
    )
    with session2:
        display = import_package(session2, package, dest_tier="hot")
    assert display == "tiny:latest"
    resolve_manifest(settings.hot_root, "tiny", settings)


def test_export_remove(sample_model: dict[str, object]) -> None:
    settings = sample_model["settings"]
    assert isinstance(settings, Settings)
    session = prepare_mutation(settings, skip_privileges=True, stop_service=False)
    with session:
        package = export_model(
            session,
            "tiny",
            source_tier="hot",
            output=settings.export_root,
            remove=True,
        )
    assert package.is_file()
    assert not (settings.hot_root / "manifests" / str(sample_model["rel"])).exists()


def test_export_dry_run(sample_model: dict[str, object]) -> None:
    settings = sample_model["settings"]
    assert isinstance(settings, Settings)
    session = prepare_mutation(settings, skip_privileges=True, stop_service=False)
    with session:
        dest = export_model(
            session,
            "tiny",
            source_tier="hot",
            output=settings.export_root,
            dry_run=True,
        )
    assert not dest.exists()
    assert (settings.hot_root / "manifests" / str(sample_model["rel"])).is_file()
