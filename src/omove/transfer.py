"""Verified blob and manifest transfers."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from omove.errors import InsufficientSpaceError, ManifestError, OmoveError
from omove.logging_util import error, info
from omove.manifest import blob_filename
from omove.system import Session

_PROGRESS_MIN_BYTES = 16 * 1024 * 1024
_SILENT = False


def set_silent(silent: bool) -> None:
    """Enable or disable progress output globally (``--silent``)."""
    global _SILENT
    _SILENT = bool(silent)


def is_silent() -> bool:
    """Return True when progress output is suppressed."""
    return _SILENT


def progress_enabled(requested: bool = True) -> bool:
    """Return whether progress should be shown for this call."""
    return bool(requested) and not _SILENT


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


def _want_progress(total: int) -> bool:
    if _SILENT:
        return False
    if total < _PROGRESS_MIN_BYTES:
        return False
    return sys.stderr.isatty() or sys.stdout.isatty()


def _render_progress(prefix: str, done: int, total: int, started: float) -> None:
    pct = 0.0 if total <= 0 else min(100.0, 100.0 * done / total)
    elapsed = max(time.monotonic() - started, 0.001)
    rate = done / elapsed
    eta = ""
    if done > 0 and done < total:
        remaining = (total - done) / rate
        eta = f" ETA {int(remaining // 60):02d}:{int(remaining % 60):02d}"
    bar_w = 24
    filled = 0 if total <= 0 else int(bar_w * done / total)
    bar = "#" * filled + "-" * (bar_w - filled)
    line = (
        f"\r{prefix} [{bar}] {pct:5.1f}% "
        f"{format_bytes(done)}/{format_bytes(total)} "
        f"{format_bytes(int(rate))}/s{eta}   "
    )
    print(line, end="", file=sys.stderr, flush=True)


def file_sha256(
    path: Path,
    *,
    progress_label: str | None = None,
) -> str:
    """Return hex sha256 of a file, with optional stderr progress."""
    total = path.stat().st_size
    show = progress_label is not None and _want_progress(total)
    digest = hashlib.sha256()
    done = 0
    started = time.monotonic()
    last_draw = 0.0
    prefix = progress_label or "Hashing"
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            done += len(chunk)
            if show:
                now = time.monotonic()
                if now - last_draw >= 0.2 or done >= total:
                    _render_progress(prefix, done, total, started)
                    last_draw = now
    if show:
        print(file=sys.stderr, flush=True)
    return digest.hexdigest()


def verify_blob(
    path: Path,
    digest: str,
    *,
    cache: dict[str, bool] | None = None,
    progress: bool = True,
) -> None:
    """Verify blob path matches digest; raise on failure."""
    cache_key = f"{path}|{digest}"
    if cache is not None and cache_key in cache:
        return
    if not path.is_file() or path.is_symlink():
        raise OmoveError(f"Missing blob: {path}")
    expected = digest.removeprefix("sha256:").lower()
    short = expected[:12]
    label = None
    show = progress_enabled(progress)
    if show:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size >= _PROGRESS_MIN_BYTES:
            info(f"Hashing blob {short} ({format_bytes(size)})...")
            label = f"Hash {short}"
    try:
        actual = file_sha256(path, progress_label=label)
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
    """Copy a file with rsync; do not preserve owner/group.

    ``-a`` includes owner/group; many network mounts reject chown even as
    root (NFS root_squash / autofs), which makes rsync exit 23 after a
    successful data transfer.
    """
    options = [
        "-a",
        "--sparse",
        "--protect-args",
        "--no-owner",
        "--no-group",
    ]
    if progress_enabled() and (sys.stdout.isatty() or sys.stderr.isatty()):
        options.append("--info=progress2")
    result = subprocess.run(
        ["rsync", *options, "--", str(source), str(destination)],
        check=False,
    )
    if result.returncode != 0:
        raise OmoveError(f"Copy failed: {source} -> {destination}")


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
    short = digest.removeprefix("sha256:")[:12]
    try:
        size = source_blob.stat().st_size
    except OSError:
        size = 0
    verify_blob(source_blob, digest, cache=cache, progress=True)

    if destination_blob.exists():
        info(f"Blob {short} already present at destination; verifying...")
        verify_blob(destination_blob, digest, cache=cache, progress=True)
        return

    if dry_run:
        info(f"[dry-run] would copy blob {short} ({format_bytes(size)})...")
        return

    info(f"Copying blob {short} ({format_bytes(size)})...")
    blobs_dir = destination_root / "blobs"
    blobs_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".omove-{short}.",
        dir=str(blobs_dir),
    )
    os.close(fd)
    temp_blob = Path(temp_name)
    session.register_temp(temp_blob)
    try:
        _rsync_copy(source_blob, temp_blob)
        info(f"Verifying copied blob {short}...")
        verify_blob(temp_blob, digest, cache=cache, progress=True)
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
        _rsync_copy(source_manifest, temp_manifest)
        if source_manifest.read_bytes() != temp_manifest.read_bytes():
            raise OmoveError(
                f"Manifest verification failed after copy: {source_manifest}"
            )
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
