"""Portable model packages for backup / cloud (.omove.tar.gz).

An export archive contains everything Ollama needs for one model:
manifest + content-addressed blobs. freeze/thaw keep models in the
hot/cold store layout; export/import move a self-contained package.
"""

from __future__ import annotations

import io
import json
import os
import re
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from omove.config import Settings
from omove.errors import OmoveError, UsageError
from omove.logging_util import error, info, log
from omove.manifest import blob_filename, load_manifest
from omove.paths import canonicalize_manifest_relpath, manifest_display_name
from omove.store import resolve_manifest, verify_manifest_blobs
from omove.system import Session
from omove.transfer import (
    format_bytes,
    prune_empty_manifest_dirs,
    verify_blob,
)
from omove.transition import garbage_collect_candidates

PACKAGE_FORMAT = "omove-package"
PACKAGE_VERSION = 1
PACKAGE_SUFFIX = ".omove.tar.gz"
META_NAME = "omove-package.json"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(display: str) -> str:
    cleaned = _SAFE_NAME.sub("_", display.replace(":", "_").replace("/", "_"))
    return cleaned.strip("._") or "model"


def default_package_path(settings: Settings, display: str, digest_prefix: str) -> Path:
    """Return default export file path under export_root."""
    name = f"{_safe_filename(display)}-{digest_prefix}{PACKAGE_SUFFIX}"
    return settings.export_root / name


def resolve_output_path(
    settings: Settings,
    display: str,
    digest_prefix: str,
    output: Path | None,
) -> Path:
    """Resolve -o path: file, directory, or default export_root."""
    filename = f"{_safe_filename(display)}-{digest_prefix}{PACKAGE_SUFFIX}"
    if output is None:
        return settings.export_root / filename
    output = output.expanduser()
    if output.exists() and output.is_dir():
        return output / filename
    if str(output).endswith(("/", os.sep)):
        path = Path(str(output).rstrip("/\\"))
        path.mkdir(parents=True, exist_ok=True)
        return path / filename
    if output.suffixes[-2:] != [".tar", ".gz"] and output.suffix != ".gz":
        # No archive suffix → treat as directory.
        output.mkdir(parents=True, exist_ok=True)
        return output / filename
    return output


def export_model(
    session: Session,
    query: str,
    *,
    source_tier: str = "hot",
    output: Path | None = None,
    remove: bool = False,
    dry_run: bool = False,
) -> Path:
    """Package one model from hot or cold into a .omove.tar.gz archive."""
    if source_tier not in {"hot", "cold"}:
        raise UsageError("export --from must be 'hot' or 'cold'.")
    settings = session.settings
    root = settings.root_for(source_tier)
    rel, canonical_rel = resolve_manifest(root, query, settings)
    display = (
        manifest_display_name(
            canonical_rel,
            default_host=settings.default_host,
            default_namespace=settings.default_namespace,
        )
        or canonical_rel
    )
    source_manifest = root / "manifests" / rel
    info(f"Validating {display} in {source_tier} storage.")
    verify_manifest_blobs(root, source_manifest)
    info_data = load_manifest(source_manifest)
    digests = list(info_data.digests)
    digest_prefix = digests[0].removeprefix("sha256:")[:12]
    dest = resolve_output_path(settings, display, digest_prefix, output)

    if dest.exists() and not dry_run:
        raise OmoveError(f"Export target already exists: {dest}")

    meta = {
        "format": PACKAGE_FORMAT,
        "format_version": PACKAGE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": display,
        "canonical_rel": canonical_rel,
        "source_rel": rel,
        "source_tier": source_tier,
        "digests": digests,
        "logical_size": info_data.logical_size,
    }

    if dry_run:
        log(
            f"[dry-run] would export {display} from {source_tier} -> {dest} "
            f"({format_bytes(info_data.logical_size)}; "
            f"{len(digests)} blob(s)"
            f"{'; remove from source' if remove else ''})"
        )
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    if not os.access(dest.parent, os.W_OK | os.X_OK):
        raise OmoveError(
            f"Cannot write export directory {dest.parent}. "
            "Fix permissions or pass -o to another path."
        )

    fd, temp_name = tempfile.mkstemp(
        prefix=".omove-export-",
        suffix=PACKAGE_SUFFIX,
        dir=str(dest.parent),
    )
    os.close(fd)
    temp_path = Path(temp_name)
    session.register_temp(temp_path)

    try:
        with tarfile.open(temp_path, "w:gz") as archive:
            meta_bytes = json.dumps(meta, indent=2).encode("utf-8")
            meta_info = tarfile.TarInfo(name=META_NAME)
            meta_info.size = len(meta_bytes)
            archive.addfile(meta_info, fileobj=io.BytesIO(meta_bytes))

            archive.add(
                source_manifest,
                arcname=f"manifests/{canonical_rel}",
                recursive=False,
            )
            for digest in digests:
                blob = root / "blobs" / blob_filename(digest)
                verify_blob(blob, digest)
                archive.add(
                    blob,
                    arcname=f"blobs/{blob_filename(digest)}",
                    recursive=False,
                )

        os.replace(temp_path, dest)
        if temp_path in session.temps:
            session.temps.remove(temp_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    size = dest.stat().st_size
    log(
        f"Exported {display} -> {dest} "
        f"(package {format_bytes(size)}; logical {format_bytes(info_data.logical_size)})"
    )

    if remove:
        info(f"Removing {display} from {source_tier} storage after export.")
        source_manifest.unlink()
        prune_empty_manifest_dirs(root / "manifests", source_manifest)
        reclaimed = garbage_collect_candidates(
            root, digests, settings
        )
        log(
            f"Removed {display} from {source_tier}; "
            f"reclaimed {format_bytes(reclaimed)}."
        )

    return dest


def _read_package_meta(archive: tarfile.TarFile) -> dict[str, object]:
    try:
        member = archive.getmember(META_NAME)
    except KeyError as exc:
        raise OmoveError(
            f"Not an omove package (missing {META_NAME})"
        ) from exc
    extracted = archive.extractfile(member)
    if extracted is None:
        raise OmoveError(f"Cannot read {META_NAME} from package")
    try:
        data = json.loads(extracted.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OmoveError(f"Invalid {META_NAME} in package") from exc
    if not isinstance(data, dict) or data.get("format") != PACKAGE_FORMAT:
        raise OmoveError("Unrecognized omove package format")
    if data.get("format_version") != PACKAGE_VERSION:
        raise OmoveError(
            f"Unsupported package format_version: {data.get('format_version')}"
        )
    return data


def import_package(
    session: Session,
    package: Path,
    *,
    dest_tier: str = "hot",
    dry_run: bool = False,
) -> str:
    """Import a .omove.tar.gz package into hot or cold storage."""
    if dest_tier not in {"hot", "cold"}:
        raise UsageError("import --to must be 'hot' or 'cold'.")
    settings = session.settings
    package = package.expanduser().resolve()
    if not package.is_file():
        raise OmoveError(f"Package not found: {package}")

    root = settings.root_for(dest_tier)
    with tarfile.open(package, "r:gz") as archive:
        meta = _read_package_meta(archive)
        canonical_rel = str(meta.get("canonical_rel") or "")
        display = str(meta.get("model") or canonical_rel)
        if not canonical_rel or canonicalize_manifest_relpath(canonical_rel) is None:
            raise OmoveError(f"Package has invalid canonical_rel: {canonical_rel!r}")

        dest_manifest = root / "manifests" / canonical_rel
        if dest_manifest.exists() and not dry_run:
            raise OmoveError(
                f"Model already exists in {dest_tier} storage: {display} "
                f"({dest_manifest})"
            )

        members = {m.name: m for m in archive.getmembers() if m.isfile()}
        manifest_name = f"manifests/{canonical_rel}"
        if manifest_name not in members:
            raise OmoveError(f"Package missing {manifest_name}")

        digests = meta.get("digests")
        if not isinstance(digests, list) or not digests:
            raise OmoveError("Package metadata missing digests")

        if dry_run:
            log(
                f"[dry-run] would import {display} from {package} -> {dest_tier} "
                f"({len(digests)} blob(s))"
            )
            return display

        # Extract to a temp dir then verify + move into place.
        with tempfile.TemporaryDirectory(prefix="omove-import-") as tmp:
            tmp_path = Path(tmp)
            # data filter is 3.12+; fall back on older Python.
            try:
                archive.extractall(tmp_path, filter="data")
            except TypeError:
                archive.extractall(tmp_path)
            extracted_manifest = tmp_path / manifest_name
            if not extracted_manifest.is_file():
                raise OmoveError("Failed to extract manifest from package")

            info_data = load_manifest(extracted_manifest)
            for digest in info_data.digests:
                blob_src = tmp_path / "blobs" / blob_filename(digest)
                if not blob_src.is_file():
                    raise OmoveError(f"Package missing blob for {digest}")
                verify_blob(blob_src, digest)

            blobs_dir = root / "blobs"
            blobs_dir.mkdir(parents=True, exist_ok=True)
            dest_manifest.parent.mkdir(parents=True, exist_ok=True)

            for digest in info_data.digests:
                blob_src = tmp_path / "blobs" / blob_filename(digest)
                blob_dest = blobs_dir / blob_filename(digest)
                if blob_dest.exists():
                    verify_blob(blob_dest, digest)
                    continue
                # Atomic place
                fd, tmp_blob_name = tempfile.mkstemp(
                    prefix=".omove-imp-", dir=str(blobs_dir)
                )
                os.close(fd)
                tmp_blob = Path(tmp_blob_name)
                try:
                    tmp_blob.write_bytes(blob_src.read_bytes())
                    verify_blob(tmp_blob, digest)
                    os.replace(tmp_blob, blob_dest)
                except Exception:
                    tmp_blob.unlink(missing_ok=True)
                    raise

            if dest_manifest.exists():
                raise OmoveError(
                    f"Manifest appeared during import: {dest_manifest}"
                )
            fd, tmp_man_name = tempfile.mkstemp(
                prefix=".omove-imp-man-", dir=str(dest_manifest.parent)
            )
            os.close(fd)
            tmp_man = Path(tmp_man_name)
            try:
                tmp_man.write_bytes(extracted_manifest.read_bytes())
                os.replace(tmp_man, dest_manifest)
            except Exception:
                tmp_man.unlink(missing_ok=True)
                raise

            verify_manifest_blobs(root, dest_manifest)

    log(f"Imported {display} into {dest_tier} storage from {package}")
    return display


def export_models(
    session: Session,
    models: list[str],
    *,
    source_tier: str = "hot",
    output: Path | None = None,
    remove: bool = False,
    dry_run: bool = False,
) -> int:
    """Export multiple models; return 0 if all succeed."""
    status = 0
    for model in models:
        try:
            export_model(
                session,
                model,
                source_tier=source_tier,
                output=output,
                remove=remove,
                dry_run=dry_run,
            )
        except OmoveError as exc:
            error(str(exc))
            status = 1
    return status


def import_packages(
    session: Session,
    packages: list[str],
    *,
    dest_tier: str = "hot",
    dry_run: bool = False,
) -> int:
    """Import multiple packages; return 0 if all succeed."""
    status = 0
    for item in packages:
        try:
            import_package(
                session,
                Path(item),
                dest_tier=dest_tier,
                dry_run=dry_run,
            )
        except OmoveError as exc:
            error(str(exc))
            status = 1
    return status
