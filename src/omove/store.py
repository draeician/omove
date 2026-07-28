"""Store enumeration, list, and verify operations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from omove.config import Settings
from omove.errors import AmbiguousModelError, ModelNotFoundError, UsageError
from omove.logging_util import error, info, log
from omove.manifest import blob_filename, load_manifest
from omove.paths import (
    canonicalize_manifest_relpath,
    manifest_display_name,
    query_matches_relpath,
)
from omove.transfer import format_bytes, progress_enabled, verify_blob


@dataclass(frozen=True)
class ListRow:
    """One row of list output."""

    name: str
    id: str
    size: str
    modified: str
    status: str
    rel: str


def iter_manifest_rels(root: Path) -> list[str]:
    """Return sorted relative manifest paths under root/manifests."""
    manifests = root / "manifests"
    if not manifests.is_dir():
        return []
    found: list[str] = []
    for path in manifests.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(manifests).as_posix()
        depth = rel.count("/") + 1
        if 2 <= depth <= 4:
            found.append(rel)
    return sorted(found)


def list_model_names(root: Path, settings: Settings) -> list[str]:
    """Return sorted display names for every valid manifest in a store."""
    names: list[str] = []
    for rel in iter_manifest_rels(root):
        name = manifest_display_name(
            rel,
            default_host=settings.default_host,
            default_namespace=settings.default_namespace,
        )
        if name is not None:
            names.append(name)
    return sorted(set(names), key=str.lower)


def resolve_manifest(
    root: Path,
    query: str,
    settings: Settings,
) -> tuple[str, str]:
    """Resolve query to (on_disk_rel, canonical_rel)."""
    manifest_root = root / "manifests"
    if not manifest_root.is_dir():
        raise ModelNotFoundError(
            f"Manifest directory does not exist: {manifest_root}"
        )
    candidates: list[str] = []
    for rel in iter_manifest_rels(root):
        canonical = canonicalize_manifest_relpath(
            rel,
            default_host=settings.default_host,
            default_namespace=settings.default_namespace,
        )
        if canonical is None:
            continue
        if query_matches_relpath(
            query,
            rel,
            default_host=settings.default_host,
            default_namespace=settings.default_namespace,
            default_tag=settings.default_tag,
        ):
            candidates.append(rel)

    tier = "hot" if root == settings.hot_root else "cold"
    if not candidates:
        raise ModelNotFoundError(
            f"Model not found in {tier} storage: {query}"
        )
    if len(candidates) > 1:
        names = [
            manifest_display_name(
                rel,
                default_host=settings.default_host,
                default_namespace=settings.default_namespace,
            )
            or rel
            for rel in candidates
        ]
        raise AmbiguousModelError(query, names)

    rel = candidates[0]
    canonical = canonicalize_manifest_relpath(
        rel,
        default_host=settings.default_host,
        default_namespace=settings.default_namespace,
    )
    assert canonical is not None
    return rel, canonical.rel


def _file_sha256_prefix(path: Path, length: int = 12) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:length]


def build_list_rows(root: Path, settings: Settings) -> list[ListRow]:
    """Build list rows for a store root."""
    rows: list[ListRow] = []
    for rel in iter_manifest_rels(root):
        manifest = root / "manifests" / rel
        canonical = canonicalize_manifest_relpath(
            rel,
            default_host=settings.default_host,
            default_namespace=settings.default_namespace,
        )
        if canonical is None:
            rows.append(
                ListRow(
                    name=rel,
                    id="-",
                    size="-",
                    modified="-",
                    status="BAD-PATH",
                    rel=rel,
                )
            )
            continue

        legacy_layout = rel != canonical.rel
        display = (
            manifest_display_name(
                rel,
                default_host=settings.default_host,
                default_namespace=settings.default_namespace,
            )
            or rel
        )
        try:
            manifest_hash = _file_sha256_prefix(manifest)
            modified = datetime.fromtimestamp(
                manifest.stat().st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%d")
        except OSError:
            manifest_hash = "-"
            modified = "-"

        try:
            info = load_manifest(manifest)
        except Exception:
            rows.append(
                ListRow(
                    name=display,
                    id=manifest_hash,
                    size="-",
                    modified=modified,
                    status="INVALID",
                    rel=rel,
                )
            )
            continue

        missing = 0
        for digest in info.digests:
            blob = root / "blobs" / blob_filename(digest)
            if not blob.is_file() or blob.is_symlink():
                missing += 1

        if missing == 0:
            status = "LEGACY" if legacy_layout else "OK"
        else:
            status = f"MISSING:{missing}"

        rows.append(
            ListRow(
                name=display,
                id=manifest_hash,
                size=format_bytes(info.logical_size),
                modified=modified,
                status=status,
                rel=rel,
            )
        )
    return rows


def list_store(
    settings: Settings,
    tier: str,
    *,
    as_json: bool = False,
) -> int:
    """Print list of models in cold or hot store."""
    if tier not in {"cold", "hot"}:
        raise UsageError("list tier must be 'cold' or 'hot'.")
    root = settings.root_for(tier)
    rows = build_list_rows(root, settings)
    if as_json:
        print(json.dumps([asdict(row) for row in rows], indent=2))
        return 0
    name_w = max([4, *(len(row.name) for row in rows)], default=4)
    id_w = max([2, *(len(row.id) for row in rows)], default=2)
    size_w = max([4, *(len(row.size) for row in rows)], default=4)
    mod_w = max([8, *(len(row.modified) for row in rows)], default=8)
    status_w = max([6, *(len(row.status) for row in rows)], default=6)
    header = (
        f"{'NAME':<{name_w}}  {'ID':<{id_w}}  {'SIZE':<{size_w}}  "
        f"{'MODIFIED':<{mod_w}}  {'STATUS':<{status_w}}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row.name:<{name_w}}  {row.id:<{id_w}}  {row.size:<{size_w}}  "
            f"{row.modified:<{mod_w}}  {row.status:<{status_w}}"
        )
    return 0


def verify_manifest_blobs(
    root: Path,
    manifest_path: Path,
    *,
    cache: dict[str, bool] | None = None,
    progress: bool = True,
) -> bool:
    """Verify all blobs referenced by a manifest."""
    info_data = load_manifest(manifest_path)
    verified = cache if cache is not None else {}
    total = len(info_data.digests)
    show = progress_enabled(progress)
    for index, digest in enumerate(info_data.digests, start=1):
        blob = root / "blobs" / blob_filename(digest)
        if show:
            try:
                size = blob.stat().st_size
            except OSError:
                size = 0
            info(
                f"Checking blob {index}/{total} "
                f"{digest.removeprefix('sha256:')[:12]} "
                f"({format_bytes(size)})"
            )
        verify_blob(blob, digest, cache=verified, progress=progress)
    return True


def verify_one(
    root: Path,
    rel: str,
    settings: Settings,
    *,
    cache: dict[str, bool] | None = None,
) -> bool:
    """Verify one manifest; print OK/BROKEN."""
    canonical = canonicalize_manifest_relpath(
        rel,
        default_host=settings.default_host,
        default_namespace=settings.default_namespace,
    )
    if canonical is None:
        return False
    display = (
        manifest_display_name(
            canonical.rel,
            default_host=settings.default_host,
            default_namespace=settings.default_namespace,
        )
        or canonical.rel
    )
    try:
        verify_manifest_blobs(root, root / "manifests" / rel, cache=cache)
    except Exception:
        log(f"BROKEN {display}")
        return False
    log(f"OK     {display}")
    return True


def verify_store(
    settings: Settings,
    tier: str,
    models: list[str],
    *,
    as_json: bool = False,
) -> int:
    """Verify models in a store; return 0 if all OK."""
    if tier not in {"cold", "hot"}:
        raise UsageError("verify tier must be 'cold' or 'hot'.")
    root = settings.root_for(tier)
    status = 0
    results: list[dict[str, str | bool]] = []
    cache: dict[str, bool] = {}

    if models:
        for query in models:
            try:
                rel, _canonical = resolve_manifest(root, query, settings)
            except (ModelNotFoundError, AmbiguousModelError) as exc:
                error(str(exc))
                status = 1
                if as_json:
                    results.append(
                        {"query": query, "ok": False, "error": str(exc)}
                    )
                continue
            ok = verify_one(root, rel, settings, cache=cache)
            if as_json:
                display = manifest_display_name(rel) or rel
                results.append({"name": display, "rel": rel, "ok": ok})
            if not ok:
                status = 1
        if as_json:
            print(json.dumps(results, indent=2))
        return status

    for rel in iter_manifest_rels(root):
        if canonicalize_manifest_relpath(rel) is None:
            error(f"Invalid manifest path: {rel}")
            status = 1
            if as_json:
                results.append({"rel": rel, "ok": False, "error": "BAD-PATH"})
            continue
        ok = verify_one(root, rel, settings, cache=cache)
        if as_json:
            display = manifest_display_name(rel) or rel
            results.append({"name": display, "rel": rel, "ok": ok})
        if not ok:
            status = 1
    if as_json:
        print(json.dumps(results, indent=2))
    return status
