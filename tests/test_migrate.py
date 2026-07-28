"""Tests for migrate layout canonicalization."""

from __future__ import annotations

from pathlib import Path

from omove.config import Settings
from omove.migrate import MigrationStats, migrate_hot_rel
from omove.system import prepare_mutation
from tests.helpers import write_blob, write_manifest


def test_migrate_hot_flat_layout(
    stores: tuple[Path, Path, Settings],
) -> None:
    hot, _cold, settings = stores
    config_data = b"cfg"
    layer_data = b"lyr"
    config_digest = write_blob(hot, config_data)
    layer_digest = write_blob(hot, layer_data)
    legacy = "registry.ollama.ai/library/tiny:latest"
    write_manifest(
        hot,
        legacy,
        config_digest=config_digest,
        layer_digests=[layer_digest],
        config_size=len(config_data),
        layer_sizes=[len(layer_data)],
    )
    session = prepare_mutation(settings, skip_privileges=True, stop_service=False)
    stats = MigrationStats()
    with session:
        migrate_hot_rel(session, legacy, stats)
    canonical = hot / "manifests" / "registry.ollama.ai/library/tiny/latest"
    assert canonical.is_file()
    assert not (hot / "manifests" / legacy).exists()
    assert stats.manifests == 1
