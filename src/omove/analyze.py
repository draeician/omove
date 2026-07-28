"""Store blob reference analysis (unique vs shared reclaim)."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from omove.config import Settings
from omove.errors import AmbiguousModelError, ModelNotFoundError, UsageError
from omove.logging_util import error, log
from omove.manifest import blob_filename, load_manifest
from omove.paths import (
    canonicalize_manifest_relpath,
    manifest_display_name,
)
from omove.store import iter_manifest_rels, resolve_manifest
from omove.transfer import format_bytes


@dataclass
class ModelRefs:
    """A model and the digests it references."""

    name: str
    rel: str
    canonical_rel: str
    digests: list[str] = field(default_factory=list)
    logical_size: int = 0


@dataclass
class StoreIndex:
    """Full reference index for one store root."""

    tier: str
    root: Path
    models: list[ModelRefs]
    digest_sizes: dict[str, int]
    digest_present: dict[str, bool]
    digest_to_models: dict[str, list[str]]


def _display(rel: str, settings: Settings) -> str:
    return (
        manifest_display_name(
            rel,
            default_host=settings.default_host,
            default_namespace=settings.default_namespace,
        )
        or rel
    )


def build_store_index(root: Path, tier: str, settings: Settings) -> StoreIndex:
    """Scan manifests and build digest ↔ model reference maps."""
    models: list[ModelRefs] = []
    digest_sizes: dict[str, int] = {}
    digest_present: dict[str, bool] = {}
    digest_to_models: dict[str, set[str]] = defaultdict(set)

    for rel in iter_manifest_rels(root):
        canonical = canonicalize_manifest_relpath(
            rel,
            default_host=settings.default_host,
            default_namespace=settings.default_namespace,
        )
        if canonical is None:
            continue
        name = _display(rel, settings)
        manifest_path = root / "manifests" / rel
        try:
            info = load_manifest(manifest_path)
        except Exception as exc:
            error(f"Skipping invalid manifest {rel}: {exc}")
            continue
        model = ModelRefs(
            name=name,
            rel=rel,
            canonical_rel=canonical.rel,
            digests=list(info.digests),
            logical_size=info.logical_size,
        )
        models.append(model)
        for digest in info.digests:
            digest_to_models[digest].add(name)
            if digest not in digest_sizes:
                blob = root / "blobs" / blob_filename(digest)
                if blob.is_file() and not blob.is_symlink():
                    digest_present[digest] = True
                    try:
                        digest_sizes[digest] = blob.stat().st_size
                    except OSError:
                        digest_sizes[digest] = 0
                else:
                    digest_present[digest] = False
                    digest_sizes[digest] = 0

    models.sort(key=lambda m: m.name.lower())
    return StoreIndex(
        tier=tier,
        root=root,
        models=models,
        digest_sizes=digest_sizes,
        digest_present=digest_present,
        digest_to_models={
            digest: sorted(names) for digest, names in digest_to_models.items()
        },
    )


def _select_models(
    index: StoreIndex,
    root: Path,
    settings: Settings,
    queries: list[str],
) -> list[ModelRefs]:
    if not queries:
        return list(index.models)
    selected: list[ModelRefs] = []
    by_rel = {m.rel: m for m in index.models}
    for query in queries:
        try:
            rel, _canonical = resolve_manifest(root, query, settings)
        except (ModelNotFoundError, AmbiguousModelError) as exc:
            error(str(exc))
            continue
        model = by_rel.get(rel)
        if model is None:
            error(f"Model not in index after resolve: {query}")
            continue
        selected.append(model)
    return selected


def _model_report(
    model: ModelRefs,
    index: StoreIndex,
) -> dict[str, object]:
    unique = 0
    shared = 0
    missing = 0
    blobs: list[dict[str, object]] = []
    for digest in model.digests:
        size = index.digest_sizes.get(digest, 0)
        present = index.digest_present.get(digest, False)
        refs = [
            name
            for name in index.digest_to_models.get(digest, [])
            if name != model.name
        ]
        if not present:
            missing += size
            status = "MISSING"
        elif refs:
            shared += size
            status = "SHARED"
        else:
            unique += size
            status = "UNIQUE"
        blobs.append(
            {
                "digest": digest,
                "short": digest.removeprefix("sha256:")[:12],
                "size": size,
                "size_human": format_bytes(size),
                "status": status,
                "shared_with": refs,
                "present": present,
            }
        )
    blobs.sort(key=lambda b: (-int(b["size"]), str(b["short"])))
    return {
        "name": model.name,
        "rel": model.rel,
        "logical_size": model.logical_size,
        "logical_human": format_bytes(model.logical_size),
        "unique_bytes": unique,
        "unique_human": format_bytes(unique),
        "shared_bytes": shared,
        "shared_human": format_bytes(shared),
        "missing_bytes": missing,
        "blobs": blobs,
    }


def _print_tree(report: dict[str, object]) -> None:
    name = report["name"]
    log(
        f"{name}  (logical {report['logical_human']}; "
        f"reclaimable if alone removed: {report['unique_human']}; "
        f"shared: {report['shared_human']})"
    )
    blobs = report["blobs"]
    assert isinstance(blobs, list)
    for index, blob in enumerate(blobs):
        assert isinstance(blob, dict)
        last = index == len(blobs) - 1
        branch = "└──" if last else "├──"
        pad = "    " if last else "│   "
        status = blob["status"]
        line = (
            f"{branch} {blob['short']}  {blob['size_human']:>10}  {status}"
        )
        if not blob["present"]:
            line += " (file missing)"
        log(line)
        shared_with = blob.get("shared_with") or []
        if isinstance(shared_with, list) and shared_with:
            for j, other in enumerate(shared_with):
                other_last = j == len(shared_with) - 1
                other_branch = "└──" if other_last else "├──"
                log(f"{pad}{other_branch} shared with {other}")


def analyze_store(
    settings: Settings,
    tier: str,
    models: list[str],
    *,
    as_json: bool = False,
) -> int:
    """Print or JSON-dump blob sharing analysis for a store."""
    if tier not in {"hot", "cold"}:
        raise UsageError("analyze tier must be 'hot' or 'cold'.")
    root = settings.root_for(tier)
    index = build_store_index(root, tier, settings)
    selected = _select_models(index, root, settings, models)
    if models and not selected:
        return 1
    if not selected:
        log(f"No models found in {tier} storage.")
        return 0

    reports = [_model_report(model, index) for model in selected]
    unique_total = sum(int(r["unique_bytes"]) for r in reports)
    shared_total = sum(int(r["shared_bytes"]) for r in reports)
    logical_total = sum(int(r["logical_size"]) for r in reports)

    # Largest shared digests among the selection (space-hunting aid).
    selected_names = {r["name"] for r in reports}
    shared_blobs: list[dict[str, object]] = []
    seen: set[str] = set()
    for report in reports:
        for blob in report["blobs"]:  # type: ignore[union-attr]
            assert isinstance(blob, dict)
            digest = str(blob["digest"])
            if digest in seen:
                continue
            if blob["status"] != "SHARED":
                continue
            seen.add(digest)
            shared_blobs.append(
                {
                    "digest": digest,
                    "short": blob["short"],
                    "size": blob["size"],
                    "size_human": blob["size_human"],
                    "referenced_by": index.digest_to_models.get(digest, []),
                }
            )
    shared_blobs.sort(key=lambda b: -int(b["size"]))

    payload = {
        "tier": tier,
        "root": str(root),
        "models": reports,
        "summary": {
            "model_count": len(reports),
            "logical_bytes": logical_total,
            "logical_human": format_bytes(logical_total),
            "unique_bytes": unique_total,
            "unique_human": format_bytes(unique_total),
            "shared_bytes": shared_total,
            "shared_human": format_bytes(shared_total),
            "note": (
                "unique_bytes would be deleted from this store if these "
                "model(s) were removed/frozen and no other local model "
                "references those blobs. shared_bytes stay while any "
                "other model still references them."
            ),
        },
        "largest_shared": shared_blobs[:20],
    }

    if as_json:
        print(json.dumps(payload, indent=2))
        return 0

    log(f"{tier} store: {root}")
    log("")
    for report in reports:
        _print_tree(report)
        log("")

    if shared_blobs:
        log("Largest shared blobs (must remove all referencers to reclaim):")
        for blob in shared_blobs[:15]:
            refs = ", ".join(str(r) for r in blob["referenced_by"])  # type: ignore[arg-type]
            log(f"  {blob['size_human']:>10}  {blob['short']}  <- {refs}")
        log("")

    log("Summary")
    log(f"  Models analyzed:     {len(reports)}")
    log(f"  Logical size:        {format_bytes(logical_total)}")
    log(f"  Unique (reclaimable): {format_bytes(unique_total)}")
    log(f"  Shared (kept):       {format_bytes(shared_total)}")
    if selected_names and unique_total == 0 and shared_total > 0:
        log(
            "  Tip: almost everything is shared — freeze/remove the other "
            "models listed under SHARED to free the large blobs."
        )
    return 0
