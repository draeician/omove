"""Verified blob and manifest transfers."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from omove.errors import InsufficientSpaceError, ManifestError, OmoveError
from omove.logging_util import error, info
from omove.manifest import blob_filename
from omove.system import Session


def format_bytes(num: int) -> str:
    """Format bytes in IEC units (approx. numfmt --to=iec-i --suffix=B)."""
    if num < 0:
        num = 0
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    value = float(num)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)}B"
            text = f"{value:.1f}".rstrip("0").rstrip(".")
            return f"{text}{unit}"
        value /= 1024.0
    return f"{int(num)}B"


def file_sha256(path: Path) -> str:
    """Return hex sha256 of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_blob(
    path: Path,
    digest: str,
    *,
    cache: dict[str, bool] | None = None,
) -> None:
    """Verify blob path matches digest; raise on failure."""
    cache_key = f"{path}|{digest}"
    if cache is not None and cache_key in cache:
        return
    if not path.is_file() or path.is_symlink():
        raise OmoveError(f"Missing blob: {path}")
    expected = digest.removeprefix("sha256:").lower()
    try:
        actual = file_sha256(path)
    except OSError as exc:
        raise OmoveError(f"Unable to hash blob: {path}") from exc
    if actual != expected:
        raise OmoveError(
            f"Blob digest mismatch for {path}: expected {expected}, "
            f"got {actual}"
        )
    if cache is not None:
        cache[cache_key] = True


def available_bytes(path: Path) -> int:
    """Return free bytes on the filesystem containing path."""
    usage = shutil.disk_usage(path)
    return int(usage.free)


def check_destination_space(
    source_root: Path,
    destination_root: Path,
    digests: list[str] | tuple[str, ...],
    *,
    cache: dict[str, bool] | None = None,
) -> None:
    """Ensure destination has room for blobs not already present."""
    required = 0
    for digest in digests:
        filename = blob_filename(digest)
        source_blob = source_root / "blobs" / filename
        destination_blob = destination_root / "blobs" / filename
        if destination_blob.exists():
            verify_blob(destination_blob, digest, cache=cache)
            continue
        try:
            required += source_blob.stat().st_size
        except OSError as exc:
            raise OmoveError(f"Unable to stat source blob: {source_blob}") from exc

    blobs_dir = destination_root / "blobs"
    try:
        available = available_bytes(blobs_dir)
    except OSError as exc:
        raise OmoveError(
            f"Unable to determine free space for {destination_root}"
        ) from exc

    reserve = 64 * 1024 * 1024
    if required // 20 > reserve:
        reserve = required // 20
    needed = 1024 * 1024 if required == 0 else required + reserve
    if available < needed:
        raise InsufficientSpaceError(
            f"Insufficient free space in {destination_root}\n"
            f"Need approximately {format_bytes(needed)}, available "
            f"{format_bytes(available)}."
        )


def _rsync_copy(source: Path, destination: Path) -> None:
    options = ["-a", "--sparse", "--protect-args"]
    if sys.stdout.isatty():
        options.append("--info=progress2")
    result = subprocess.run(
        ["rsync", *options, "--", str(source), str(destination)],
        check=False,
    )
    if result.returncode != 0:
        raise OmoveError(f"Blob copy failed: {source}")


def copy_blob_verified(
    session: Session,
    source_root: Path,
    destination_root: Path,
    digest: str,
    *,
    cache: dict[str, bool] | None = None,
    dry_run: bool = False,
) -> None:
    """Copy a blob with content verification and atomic rename."""
    filename = blob_filename(digest)
    source_blob = source_root / "blobs" / filename
    destination_blob = destination_root / "blobs" / filename
    verify_blob(source_blob, digest, cache=cache)

    if destination_blob.exists():
        verify_blob(destination_blob, digest, cache=cache)
        return

    if dry_run:
        info(f"[dry-run] would copy blob {digest[7:19]}...")
        return

    info(f"Copying blob {digest[7:19]}...")
    blobs_dir = destination_root / "blobs"
    blobs_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".omove-{digest[7:19]}.",
        dir=str(blobs_dir),
    )
    os.close(fd)
    temp_blob = Path(temp_name)
    session.register_temp(temp_blob)
    try:
        _rsync_copy(source_blob, temp_blob)
        session.chown_path(temp_blob)
        os.chmod(temp_blob, 0o644)
        verify_blob(temp_blob, digest, cache=cache)
        os.replace(temp_blob, destination_blob)
        if temp_blob in session.temps:
            session.temps.remove(temp_blob)
    except Exception:
        temp_blob.unlink(missing_ok=True)
        raise
    if cache is not None:
        cache[f"{destination_blob}|{digest}"] = True


def copy_manifest_verified(
    session: Session,
    source_manifest: Path,
    destination_manifest: Path,
    *,
    dry_run: bool = False,
) -> None:
    """Copy a manifest atomically with byte compare."""
    destination_dir = destination_manifest.parent
    if dry_run:
        info(f"[dry-run] would copy manifest to {destination_manifest}")
        return

    destination_dir.mkdir(parents=True, exist_ok=True)
    session.chown_path(destination_dir)
    os.chmod(destination_dir, 0o755)

    if destination_manifest.exists():
        if not destination_manifest.is_file() or destination_manifest.is_symlink():
            raise ManifestError(
                f"Destination manifest is not a regular file: "
                f"{destination_manifest}"
            )
        if source_manifest.read_bytes() != destination_manifest.read_bytes():
            raise OmoveError(
                f"A different manifest already exists at {destination_manifest}"
            )
        return

    fd, temp_name = tempfile.mkstemp(
        prefix=".omove-manifest.",
        dir=str(destination_dir),
    )
    os.close(fd)
    temp_manifest = Path(temp_name)
    session.register_temp(temp_manifest)
    try:
        result = subprocess.run(
            [
                "rsync",
                "-a",
                "--protect-args",
                "--",
                str(source_manifest),
                str(temp_manifest),
            ],
            check=False,
        )
        if result.returncode != 0:
            raise OmoveError(f"Manifest copy failed: {source_manifest}")
        if source_manifest.read_bytes() != temp_manifest.read_bytes():
            raise OmoveError(
                f"Manifest verification failed after copy: {source_manifest}"
            )
        session.chown_path(temp_manifest)
        os.chmod(temp_manifest, 0o644)
        os.replace(temp_manifest, destination_manifest)
        if temp_manifest in session.temps:
            session.temps.remove(temp_manifest)
    except Exception:
        temp_manifest.unlink(missing_ok=True)
        raise


def prune_empty_manifest_dirs(manifest_root: Path, removed_manifest: Path) -> None:
    """Remove empty parent dirs up to manifest_root."""
    directory = removed_manifest.parent
    while directory != manifest_root and manifest_root in directory.parents:
        try:
            directory.rmdir()
        except OSError:
            break
        directory = directory.parent
