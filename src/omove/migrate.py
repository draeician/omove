"""Layout migration and cold-store repair from hot blobs."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from omove.errors import OmoveError, UsageError
from omove.logging_util import error, log
from omove.manifest import blob_filename, load_manifest
from omove.paths import canonicalize_manifest_relpath, manifest_display_name
from omove.store import (
    iter_manifest_rels,
    resolve_manifest,
    verify_manifest_blobs,
)
from omove.system import Session
from omove.transfer import (
    check_destination_space,
    copy_blob_verified,
    prune_empty_manifest_dirs,
)


@dataclass
class MigrationStats:
    """Counters for a migrate run."""

    manifests: int = 0
    blobs: int = 0
    unresolved: int = 0

    def summary(self) -> str:
        return (
            f"Migration summary: {self.manifests} manifest(s) canonicalized; "
            f"{self.blobs} blob(s) restored from hot storage; "
            f"{self.unresolved} model(s) remain incomplete."
        )


def move_manifest_to_canonical(
    session: Session,
    root: Path,
    rel: str,
    stats: MigrationStats,
    *,
    dry_run: bool = False,
) -> str:
    """Move a legacy layout manifest to canonical path; return new rel."""
    settings = session.settings
    canonical = canonicalize_manifest_relpath(
        rel,
        default_host=settings.default_host,
        default_namespace=settings.default_namespace,
    )
    if canonical is None:
        raise OmoveError(f"Cannot migrate invalid manifest path: {rel}")
    if rel == canonical.rel:
        return rel

    source_manifest = root / "manifests" / rel
    destination_manifest = root / "manifests" / canonical.rel
    display = (
        manifest_display_name(
            canonical.rel,
            default_host=settings.default_host,
            default_namespace=settings.default_namespace,
        )
        or canonical.rel
    )

    if dry_run:
        log(f"[dry-run] MIGRATED {display}")
        stats.manifests += 1
        return canonical.rel

    destination_dir = destination_manifest.parent
    destination_dir.mkdir(parents=True, exist_ok=True)

    if destination_manifest.exists():
        if (
            not destination_manifest.is_file()
            or destination_manifest.is_symlink()
        ):
            raise OmoveError(
                f"Canonical manifest destination is not a regular file: "
                f"{destination_manifest}"
            )
        if source_manifest.read_bytes() != destination_manifest.read_bytes():
            raise OmoveError(
                f"Conflicting canonical manifest already exists for {display}\n"
                f"Legacy:    {source_manifest}\n"
                f"Canonical: {destination_manifest}"
            )
        source_manifest.unlink()
    else:
        source_manifest.rename(destination_manifest)

    prune_empty_manifest_dirs(root / "manifests", source_manifest)
    stats.manifests += 1
    log(f"MIGRATED {display}")
    return canonical.rel


def repair_cold_manifest_from_hot(
    session: Session,
    rel: str,
    stats: MigrationStats,
    *,
    dry_run: bool = False,
    cache: dict[str, bool] | None = None,
) -> None:
    """Copy missing cold blobs from hot when available."""
    settings = session.settings
    canonical = canonicalize_manifest_relpath(
        rel,
        default_host=settings.default_host,
        default_namespace=settings.default_namespace,
    )
    if canonical is None:
        raise OmoveError(f"Invalid cold manifest path: {rel}")
    display = (
        manifest_display_name(
            canonical.rel,
            default_host=settings.default_host,
            default_namespace=settings.default_namespace,
        )
        or canonical.rel
    )
    manifest = settings.cold_root / "manifests" / rel
    info_data = load_manifest(manifest)
    verified = cache if cache is not None else {}
    copyable: list[str] = []
    unresolved: list[str] = []

    for digest in info_data.digests:
        filename = blob_filename(digest)
        cold_blob = settings.cold_root / "blobs" / filename
        hot_blob = settings.hot_root / "blobs" / filename
        if cold_blob.exists():
            if not cold_blob.is_file() or cold_blob.is_symlink():
                raise OmoveError(
                    f"Cold blob path is not a regular file: {cold_blob}"
                )
            continue
        if hot_blob.is_file() and not hot_blob.is_symlink():
            copyable.append(digest)
        else:
            unresolved.append(digest)

    if copyable:
        check_destination_space(
            settings.hot_root, settings.cold_root, copyable, cache=verified
        )
        for digest in copyable:
            copy_blob_verified(
                session,
                settings.hot_root,
                settings.cold_root,
                digest,
                cache=verified,
                dry_run=dry_run,
            )
            stats.blobs += 1
        prefix = "[dry-run] " if dry_run else ""
        log(
            f"{prefix}REPAIRED {display} ({len(copyable)} blob(s) copied "
            "from hot storage)"
        )

    if unresolved:
        error(
            f"{display} still has {len(unresolved)} blob(s) unavailable in "
            "both cold and hot storage:"
        )
        for digest in unresolved:
            print(f"  {digest}", file=sys.stderr)
        stats.unresolved += 1
        raise OmoveError(f"{display} remains incomplete after repair.")


def migrate_cold_rel(
    session: Session,
    rel: str,
    stats: MigrationStats,
    *,
    dry_run: bool = False,
    cache: dict[str, bool] | None = None,
) -> None:
    """Canonicalize a cold manifest and repair missing blobs."""
    settings = session.settings
    if canonicalize_manifest_relpath(
        rel,
        default_host=settings.default_host,
        default_namespace=settings.default_namespace,
    ) is None:
        raise OmoveError(f"Invalid cold manifest path: {rel}")
    current = move_manifest_to_canonical(
        session, settings.cold_root, rel, stats, dry_run=dry_run
    )
    repair_cold_manifest_from_hot(
        session, current, stats, dry_run=dry_run, cache=cache
    )


def migrate_hot_rel(
    session: Session,
    rel: str,
    stats: MigrationStats,
    *,
    dry_run: bool = False,
    cache: dict[str, bool] | None = None,
) -> None:
    """Canonicalize a hot manifest only when blobs verify."""
    settings = session.settings
    canonical = canonicalize_manifest_relpath(
        rel,
        default_host=settings.default_host,
        default_namespace=settings.default_namespace,
    )
    if canonical is None:
        raise OmoveError(f"Invalid hot manifest path: {rel}")
    if rel == canonical.rel:
        return
    verify_manifest_blobs(
        settings.hot_root,
        settings.hot_root / "manifests" / rel,
        cache=cache,
    )
    move_manifest_to_canonical(
        session, settings.hot_root, rel, stats, dry_run=dry_run
    )


def migrate_store(
    session: Session,
    tier: str,
    models: list[str],
    stats: MigrationStats,
    *,
    dry_run: bool = False,
) -> int:
    """Migrate one tier; return 0 on full success."""
    if tier not in {"cold", "hot"}:
        raise UsageError("migrate tier must be 'cold' or 'hot'.")
    settings = session.settings
    root = settings.root_for(tier)
    status = 0
    cache: dict[str, bool] = {}

    if models:
        for query in models:
            try:
                rel, _canonical = resolve_manifest(root, query, settings)
            except OmoveError as exc:
                error(str(exc))
                status = 1
                continue
            try:
                if tier == "cold":
                    migrate_cold_rel(
                        session, rel, stats, dry_run=dry_run, cache=cache
                    )
                else:
                    migrate_hot_rel(
                        session, rel, stats, dry_run=dry_run, cache=cache
                    )
            except OmoveError as exc:
                error(str(exc))
                status = 1
        return status

    rels = iter_manifest_rels(root)
    for rel in rels:
        if not (root / "manifests" / rel).exists():
            continue
        try:
            if tier == "cold":
                migrate_cold_rel(
                    session, rel, stats, dry_run=dry_run, cache=cache
                )
            else:
                migrate_hot_rel(
                    session, rel, stats, dry_run=dry_run, cache=cache
                )
        except OmoveError as exc:
            error(str(exc))
            status = 1
    return status


def run_migrate(
    session: Session,
    tier: str,
    models: list[str],
    *,
    dry_run: bool = False,
) -> int:
    """Run migrate for all/hot/cold and print summary."""
    if tier == "all" and models:
        raise UsageError(
            "Model selection requires an explicit migrate tier: cold or hot."
        )
    stats = MigrationStats()
    status = 0
    tiers = ["hot", "cold"] if tier == "all" else [tier]
    for current in tiers:
        if migrate_store(
            session, current, models, stats, dry_run=dry_run
        ) != 0:
            status = 1
    log(stats.summary())
    return status
