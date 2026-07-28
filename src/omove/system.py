"""Privileges, locking, mounts, and Ollama service lifecycle."""

from __future__ import annotations

import errno
import fcntl
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType

from omove.config import Settings
from omove.errors import StoreUnsafeError
from omove.logging_util import error, info, warn


def require_commands(*names: str) -> None:
    """Ensure required external commands are on PATH."""
    missing = False
    for name in names:
        if shutil.which(name) is None:
            error(f"Required command not found: {name}")
            missing = True
    if missing:
        raise StoreUnsafeError("Missing required commands.")


def systemctl_argv(*args: str) -> list[str]:
    """Build a systemctl argv, prefixing ``sudo`` when not root.

    Matches fine-grained sudoers aliases (Option B) that allow only
    ``systemctl`` for the Ollama unit — never a full process re-exec.
    """
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        raise StoreUnsafeError("Required command not found: systemctl")
    if os.geteuid() == 0:
        return [systemctl, *args]
    sudo = shutil.which("sudo")
    if sudo is None:
        raise StoreUnsafeError(
            "sudo is required to control the Ollama systemd unit. "
            "Install sudo, or run as root."
        )
    return [sudo, systemctl, *args]


def run_systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run systemctl (via sudo when needed)."""
    return subprocess.run(
        systemctl_argv(*args),
        check=False,
        capture_output=True,
        text=True,
    )


def validate_roots(settings: Settings) -> None:
    """Refuse identical or nested hot/cold roots."""
    hot = settings.hot_root
    cold = settings.cold_root
    if hot == Path("/"):
        raise StoreUnsafeError("Hot root cannot be /.")
    if cold == Path("/"):
        raise StoreUnsafeError("Cold root cannot be /.")
    if hot == cold:
        raise StoreUnsafeError(
            f"Hot and cold roots resolve to the same directory: {hot}"
        )
    hot_s = f"{hot}/"
    cold_s = f"{cold}/"
    if hot_s.startswith(cold_s):
        raise StoreUnsafeError("Hot storage cannot be inside cold storage.")
    if cold_s.startswith(hot_s):
        raise StoreUnsafeError("Cold storage cannot be inside hot storage.")


def filesystem_mount(path: Path) -> Path | None:
    """Return the mount TARGET that contains *path*, if detectable."""
    if shutil.which("findmnt") is None:
        return None
    probe = path if path.exists() else path.parent
    result = subprocess.run(
        ["findmnt", "-n", "-o", "TARGET", "--target", str(probe)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    lines = result.stdout.strip().splitlines()
    if not lines:
        return None
    return Path(lines[0].strip())


def validate_cold_mount(settings: Settings) -> None:
    """Ensure cold storage is on a real mounted disk, not root ``/``.

    Classic failure: a removable/NAS path is configured but not mounted, so
    the "archive" is an empty folder on the system disk and filling it can
    exhaust ``/``.

    We detect the mount that contains ``cold_path`` via ``findmnt``.
    ``cold_mount`` is optional: if it *is* a mountpoint, cold storage must
    live on that mount. If it is just a normal parent directory (common when
    the archive is nested under a deeper path), it is ignored.
    """
    if settings.allow_unmounted_cold:
        return

    cold = settings.cold_root
    probe = cold if cold.exists() else cold.parent
    if not probe.is_dir():
        raise StoreUnsafeError(
            "Cold archive path does not exist yet and its parent is missing: "
            f"{cold}"
        )

    actual = filesystem_mount(probe)
    if actual is None:
        mount = settings.cold_mount
        if not mount.is_dir():
            raise StoreUnsafeError(
                f"Cannot find the disk mount for cold archive {cold}, and "
                f"cold_mount path does not exist: {mount}"
            )
        result = subprocess.run(
            ["mountpoint", "-q", "--", str(mount)],
            check=False,
            capture_output=True,
        )
        if result.returncode != 0:
            raise StoreUnsafeError(
                f"Cannot verify that cold archive {cold} is on a mounted "
                "disk.\n"
                "Install findmnt, set cold_mount to the disk mount "
                f"(try: findmnt -n -o TARGET --target {probe}), or set "
                "allow_unmounted_cold = true if you intentionally keep the "
                "archive on this filesystem."
            )
        return

    if actual == Path("/"):
        raise StoreUnsafeError(
            f"Cold archive {cold} is on the root filesystem (/).\n"
            "That often means a dedicated disk is not mounted, and writing "
            "here can fill your system disk.\n"
            "Fix: put the archive on another disk, or set "
            "allow_unmounted_cold = true in ~/.config/omove/config.toml "
            "(or OMOVE_ALLOW_UNMOUNTED_COLD=1) if you really mean to use /."
        )

    configured = settings.cold_mount
    configured_is_mount = (
        subprocess.run(
            ["mountpoint", "-q", "--", str(configured)],
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )
    if configured_is_mount and actual != configured:
        try:
            cold.resolve().relative_to(configured.resolve())
        except ValueError as exc:
            raise StoreUnsafeError(
                f"Cold archive {cold} is on mount {actual}, but config "
                f"cold_mount is {configured}.\n"
                f'Set cold_mount = "{actual}" (or omit it), or set '
                "allow_unmounted_cold = true."
            ) from exc


def ensure_hot_store(settings: Settings) -> None:
    """Require a complete, readable hot store (no symlink dirs)."""
    hot = settings.hot_root
    manifests = hot / "manifests"
    blobs = hot / "blobs"
    if not manifests.is_dir() or not blobs.is_dir():
        raise StoreUnsafeError(
            f"Hot Ollama model store is incomplete or missing: {hot}"
        )
    if manifests.is_symlink() or blobs.is_symlink():
        raise StoreUnsafeError(
            "Hot manifests or blobs directory is a symbolic link. "
            "Refusing to continue."
        )
    for path in (hot, manifests, blobs):
        if not os.access(path, os.R_OK | os.X_OK):
            raise StoreUnsafeError(
                f"Cannot read hot store path {path}. Fix permissions and retry."
            )


def ensure_cold_store(settings: Settings, *, writable: bool = True) -> None:
    """Ensure cold store directories exist and are accessible.

    Does not change ownership. Permission problems are reported so the user
    can fix them.
    """
    validate_cold_mount(settings)
    cold = settings.cold_root
    for path in (cold, cold / "manifests", cold / "blobs"):
        try:
            path.mkdir(mode=0o755, parents=True, exist_ok=True)
        except OSError as exc:
            raise StoreUnsafeError(
                f"Cannot create cold store path {path}: {exc}"
            ) from exc
    manifests = cold / "manifests"
    blobs = cold / "blobs"
    if manifests.is_symlink() or blobs.is_symlink():
        raise StoreUnsafeError(
            "Cold manifests or blobs directory is a symbolic link. "
            "Refusing to continue."
        )
    mode = os.R_OK | os.X_OK | (os.W_OK if writable else 0)
    need = "read/write" if writable else "read"
    for path in (cold, manifests, blobs):
        if not os.access(path, mode):
            raise StoreUnsafeError(
                f"Cannot {need} cold store path {path}. "
                "Fix permissions and retry."
            )


def lock_candidates(preferred: Path) -> list[Path]:
    """Ordered lock paths: configured, then user-writable fallbacks."""
    paths = [preferred]
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        paths.append(Path(runtime) / "omove.lock")
    paths.append(Path.home() / ".cache" / "omove" / "omove.lock")
    # Preserve order, drop duplicates.
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        key = path.resolve() if path.parent.exists() else path
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def open_lock_file(lock_file: Path) -> tuple[Path, int]:
    """Open a usable lock file, falling back if the preferred path fails."""
    errors: list[str] = []
    for path in lock_candidates(lock_file):
        try:
            path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            continue
        if path != lock_file:
            warn(
                f"Cannot use lock_file {lock_file}; using {path} instead."
            )
        return path, fd
    detail = "; ".join(errors) if errors else "no candidates"
    raise StoreUnsafeError(f"Cannot open lock file ({detail})")


def acquire_lock(lock_file: Path) -> int:
    """Acquire an exclusive flock; return the open fd.

    Prints a waiting message if the lock is held. Ctrl+C cancels cleanly.
    """
    _path, fd = open_lock_file(lock_file)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            info(f"Waiting for lock ({_path}); Ctrl+C to cancel.")
        except OSError as exc:
            if getattr(exc, "errno", None) not in {
                errno.EACCES,
                errno.EAGAIN,
                errno.EWOULDBLOCK,
            }:
                raise StoreUnsafeError(
                    f"Unable to acquire lock: {_path}"
                ) from exc
            info(f"Waiting for lock ({_path}); Ctrl+C to cancel.")
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                time.sleep(0.25)
            except OSError as exc:
                if getattr(exc, "errno", None) in {
                    errno.EACCES,
                    errno.EAGAIN,
                    errno.EWOULDBLOCK,
                }:
                    time.sleep(0.25)
                    continue
                raise StoreUnsafeError(
                    f"Unable to acquire lock: {_path}"
                ) from exc
    except KeyboardInterrupt:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def stop_ollama_for_mutation(
    settings: Settings,
    *,
    systemctl: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    pgrep: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> bool:
    """Stop systemd Ollama if active. Return True if we stopped it."""
    run_ctl = systemctl or (
        lambda *args: run_systemctl(*args)
    )
    run_pgrep = pgrep or (
        lambda *args: subprocess.run(
            list(args), check=False, capture_output=True, text=True
        )
    )
    service_was_active = False
    if shutil.which("systemctl"):
        # No --quiet: keeps sudoers rules simple (exact argv match).
        active = run_ctl("is-active", settings.ollama_service)
        if active.returncode == 0:
            info(
                f"Stopping {settings.ollama_service} for a consistent "
                "storage transaction."
            )
            stopped = run_ctl("stop", settings.ollama_service)
            if stopped.returncode != 0:
                raise StoreUnsafeError(
                    f"Failed to stop {settings.ollama_service}."
                )
            service_was_active = True

    live = run_pgrep("pgrep", "-x", "ollama")
    if live.returncode == 0:
        if not settings.allow_live_ollama:
            raise StoreUnsafeError(
                "An Ollama process is still running. Refusing to modify "
                "its model store. Stop it first, or explicitly set "
                "OMOVE_ALLOW_LIVE_OLLAMA=1."
            )
        warn(
            "Proceeding while Ollama is running because "
            "OMOVE_ALLOW_LIVE_OLLAMA=1."
        )
    return service_was_active


def start_ollama_service(settings: Settings) -> bool:
    """Start the Ollama systemd unit. Return False on failure."""
    result = run_systemctl("start", settings.ollama_service)
    if result.returncode != 0:
        error(
            f"Failed to restart {settings.ollama_service}. Start it manually."
        )
        return False
    return True


@dataclass
class Session:
    """Holds lock, temps, and optional service restart."""

    settings: Settings
    lock_fd: int
    service_was_active: bool = False
    temps: list[Path] = field(default_factory=list)
    skip_privileges: bool = False

    def register_temp(self, path: Path) -> Path:
        """Track a temporary path for cleanup."""
        self.temps.append(path)
        return path

    def close(self, *, success: bool) -> int:
        """Clean temps, restart service if needed, release lock."""
        rc = 0 if success else 1
        for path in self.temps:
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                elif path.exists() or path.is_symlink():
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        self.temps.clear()
        if self.service_was_active:
            if not start_ollama_service(self.settings):
                rc = 1 if rc == 0 else rc
            self.service_was_active = False
        if self.lock_fd >= 0:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self.lock_fd)
            except OSError:
                pass
            self.lock_fd = -1
        return rc

    def __enter__(self) -> Session:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Treat interrupt as unsuccessful so Ollama still restarts if needed.
        self.close(success=exc_type is None)


def prepare_read_operation(
    settings: Settings,
    *,
    argv: Sequence[str] | None = None,
    skip_privileges: bool = False,
    cold_writable: bool = False,
) -> Session:
    """Validate stores and acquire lock for read operations.

    Does not elevate the process. Store paths must be readable (and cold
    writable when requested) by the invoking user.
    """
    del argv  # Kept for call-site compatibility; no longer used for re-exec.
    if skip_privileges:
        validate_roots(settings)
        ensure_hot_store(settings)
        ensure_cold_store(settings, writable=True)
        return Session(
            settings=settings,
            lock_fd=-1,
            skip_privileges=True,
        )

    require_commands(
        "readlink",
        "flock",
        "find",
        "sort",
        "sha256sum",
        "stat",
        "numfmt",
        "date",
        "findmnt",
        "mountpoint",
        "install",
    )
    validate_roots(settings)
    ensure_hot_store(settings)
    ensure_cold_store(settings, writable=cold_writable)
    lock_fd = acquire_lock(settings.lock_file)
    return Session(
        settings=settings,
        lock_fd=lock_fd,
        skip_privileges=False,
    )


def prepare_mutation(
    settings: Settings,
    *,
    argv: Sequence[str] | None = None,
    skip_privileges: bool = False,
    stop_service: bool = True,
) -> Session:
    """Prepare a mutation session (lock + optional Ollama stop)."""
    session = prepare_read_operation(
        settings,
        argv=argv,
        skip_privileges=skip_privileges,
        cold_writable=True,
    )
    if not skip_privileges:
        require_commands("rsync", "df", "pgrep", "sync")
    if stop_service and not skip_privileges:
        session.service_was_active = stop_ollama_for_mutation(settings)
    return session
