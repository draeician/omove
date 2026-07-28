"""Freeze/thaw transitions and blob garbage collection."""

from __future__ import annotations

from pathlib import Path

from omove.config import Settings
from omove.errors import OmoveError
from omove.logging_util import error, info, log, warn
from omove.manifest import blob_filename, digests_for_gc, load_manifest
from omove.paths import (
    canonicalize_manifest_relpath,
    legacy_relpath_for_canonical,
    manifest_display_name,
)
from omove.store import iter_manifest_rels, resolve_manifest, verify_manifest_blobs
from omove.system import Session
from omove.transfer import (
    check_destination_space,
    copy_blob_verified,
    copy_manifest_verified,
    file_sha256,
    format_bytes,
    prune_empty_manifest_dirs,
)


def build_reference_set(root: Path, settings: Settings) -> set[str]:
    """Build the set of digests still referenced by manifests."""
    references: set[str] = set()
    for rel in iter_manifest_rels(root):
        if canonicalize_manifest_relpath(
            rel,
            default_host=settings.default_host,
            default_namespace=settings.default_namespace,
        ) is None:
            raise OmoveError(
                f"Invalid manifest path found under {root / 'manifests'}: {rel}"
            )
        references.update(digests_for_gc(root / "manifests" / rel))
    return references


def garbage_collect_candidates(
    root: Path,
    digests: list[str] | tuple[str, ...],
    settings: Settings,
    *,
    cache: dict[str, bool] | None = None,
    dry_run: bool = False,
) -> int:
    """Delete unreferenced candidate blobs; return reclaimed bytes."""
    references = build_reference_set(root, settings)
    reclaimed = 0
    for digest in digests:
        if digest in references:
            continue
        blob = root / "blobs" / blob_filename(digest)
        if not blob.exists():
            continue
        if not blob.is_file() or blob.is_symlink():
            raise OmoveError(f"Refusing to remove non-regular blob path: {blob}")
        size = blob.stat().st_size
        if dry_run:
            info(f"[dry-run] would remove unreferenced blob {blob.name}")
            reclaimed += size
            continue
        blob.unlink()
        reclaimed += size
        if cache is not None:
            cache.pop(f"{blob}|{digest}", None)
    return reclaimed


def transition_model(
    session: Session,
    operation: str,
    query: str,
    *,
    dry_run: bool = False,
    cache: dict[str, bool] | None = None,
) -> None:
    """Freeze or thaw a single model between hot and cold stores."""
    settings = session.settings
    if operation == "freeze":
        source_root = settings.hot_root
        destination_root = settings.cold_root
        action_past = "Frozen"
        action_direction = "cold storage"
    elif operation == "thaw":
        source_root = settings.cold_root
        destination_root = settings.hot_root
        action_past = "Thawed"
        action_direction = "hot storage"
    else:
        raise OmoveError(f"Internal error: unknown transition operation {operation}")

    verified = cache if cache is not None else {}
    rel, canonical_rel = resolve_manifest(source_root, query, settings)
    destination_rel = canonical_rel

    if operation == "freeze":
        legacy_rel = legacy_relpath_for_canonical(
            canonical_rel,
            default_host=settings.default_host,
            default_namespace=settings.default_namespace,
        )
        if legacy_rel is not None:
            legacy_path = destination_root / "manifests" / legacy_rel
            canonical_path = destination_root / "manifests" / canonical_rel
            if legacy_path.exists():
                if canonical_path.exists():
                    display = manifest_display_name(canonical_rel) or canonical_rel
                    raise OmoveError(
                        f"Both legacy and canonical cold manifests exist for "
                        f"{display}. Resolve the duplicate archive entries "
                        "before freezing this model."
                    )
                destination_rel = legacy_rel

    source_manifest = source_root / "manifests" / rel
    destination_manifest = destination_root / "manifests" / destination_rel
    display = (
        manifest_display_name(
            canonical_rel,
            default_host=settings.default_host,
            default_namespace=settings.default_namespace,
        )
        or canonical_rel
    )

    info(f"Validating {display} in source storage.")
    verify_manifest_blobs(
        source_root, source_manifest, cache=verified, progress=True
    )
    info_data = load_manifest(source_manifest)
    digests = list(info_data.digests)
    logical_size = info_data.logical_size
    info(
        f"Source OK for {display}: {len(digests)} blob(s), "
        f"logical {format_bytes(logical_size)}."
    )
    source_manifest_hash = file_sha256(source_manifest)

    info("Checking free space on destination...")
    check_destination_space(
        source_root, destination_root, digests, cache=verified
    )

    total_blobs = len(digests)
    for index, digest in enumerate(digests, start=1):
        info(f"Transfer blob {index}/{total_blobs}...")
        copy_blob_verified(
            session,
            source_root,
            destination_root,
            digest,
            cache=verified,
            dry_run=dry_run,
        )

    if not dry_run:
        current = file_sha256(source_manifest)
        if current != source_manifest_hash:
            raise OmoveError(
                f"Source manifest changed during the transaction: "
                f"{source_manifest}\nNo source data was removed. Retry "
                "the operation."
            )

    copy_manifest_verified(
        session,
        source_manifest,
        destination_manifest,
        dry_run=dry_run,
    )
    if not dry_run:
        info(f"Verifying {display} in destination storage...")
        verify_manifest_blobs(
            destination_root,
            destination_manifest,
            cache=verified,
            progress=True,
        )
        current = file_sha256(source_manifest)
        if current != source_manifest_hash:
            raise OmoveError(
                f"Source manifest changed before commit: {source_manifest}\n"
                "Both storage tiers retain a complete copy. Retry the "
                "operation."
            )
        try:
            source_manifest.unlink()
        except OSError as exc:
            raise OmoveError(
                f"Failed to remove source manifest after destination commit: "
                f"{source_manifest}"
            ) from exc
        prune_empty_manifest_dirs(source_root / "manifests", source_manifest)

    info("Cleaning up unreferenced source blobs...")
    try:
        reclaimed = garbage_collect_candidates(
            source_root,
            digests,
            settings,
            cache=verified,
            dry_run=dry_run,
        )
    except OmoveError:
        warn(
            f"{display} was transitioned, but source blob cleanup was skipped "
            "because reference validation failed."
        )
        reclaimed = 0

    prefix = "[dry-run] " if dry_run else ""
    log(
        f"{prefix}{action_past} {display} to {action_direction}. "
        f"Logical size: {format_bytes(logical_size)}; "
        f"source space reclaimed: {format_bytes(reclaimed)}."
    )


def transition_models(
    session: Session,
    operation: str,
    models: list[str],
    *,
    dry_run: bool = False,
) -> int:
    """Freeze/thaw multiple models; return 0 if all succeed."""
    status = 0
    cache: dict[str, bool] = {}
    for model in models:
        try:
            transition_model(
                session, operation, model, dry_run=dry_run, cache=cache
            )
        except OmoveError as exc:
            error(str(exc))
            status = 1
    return status
