"""Privileges, locking, mounts, and Ollama service lifecycle."""

from __future__ import annotations

import fcntl
import os
import pwd
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType

from omove.config import PRESERVE_ENV, Settings
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


def require_root(argv: Sequence[str] | None = None) -> None:
    """Re-exec under sudo when not running as root."""
    if os.geteuid() == 0:
        return
    if shutil.which("sudo") is None:
        raise StoreUnsafeError(
            "This command requires root privileges and sudo is unavailable."
        )
    args = list(argv if argv is not None else sys.argv)
    preserve = ",".join(PRESERVE_ENV)
    os.execvp(
        "sudo",
        ["sudo", f"--preserve-env={preserve}", "--", sys.executable, *args],
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
    """Require a complete non-symlinked hot store."""
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


def ollama_ids(settings: Settings) -> tuple[int, int]:
    """Return (uid, gid) for the Ollama service account."""
    try:
        pw = pwd.getpwnam(settings.ollama_user)
    except KeyError as exc:
        raise StoreUnsafeError(
            f"Ollama user does not exist: {settings.ollama_user}"
        ) from exc
    return pw.pw_uid, pw.pw_gid


def ensure_cold_store(settings: Settings) -> tuple[int, int]:
    """Ensure cold store directories exist with correct ownership."""
    validate_cold_mount(settings)
    uid, gid = ollama_ids(settings)
    cold = settings.cold_root
    for path in (cold, cold / "manifests", cold / "blobs"):
        path.mkdir(mode=0o755, parents=True, exist_ok=True)
        os.chown(path, uid, gid)
        os.chmod(path, 0o755)
    manifests = cold / "manifests"
    blobs = cold / "blobs"
    if manifests.is_symlink() or blobs.is_symlink():
        raise StoreUnsafeError(
            "Cold manifests or blobs directory is a symbolic link. "
            "Refusing to continue."
        )
    return uid, gid


def acquire_lock(lock_file: Path) -> int:
    """Acquire an exclusive flock; return the open fd."""
    lock_file.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:
        os.close(fd)
        raise StoreUnsafeError(f"Unable to acquire lock: {lock_file}") from exc
    return fd


def stop_ollama_for_mutation(
    settings: Settings,
    *,
    systemctl: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    pgrep: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> bool:
    """Stop systemd Ollama if active. Return True if we stopped it."""
    run_systemctl = systemctl or (
        lambda *args: subprocess.run(
            list(args), check=False, capture_output=True, text=True
        )
    )
    run_pgrep = pgrep or (
        lambda *args: subprocess.run(
            list(args), check=False, capture_output=True, text=True
        )
    )
    service_was_active = False
    if shutil.which("systemctl"):
        active = run_systemctl(
            "systemctl", "is-active", "--quiet", settings.ollama_service
        )
        if active.returncode == 0:
            info(
                f"Stopping {settings.ollama_service} for a consistent "
                "storage transaction."
            )
            stopped = run_systemctl(
                "systemctl", "stop", settings.ollama_service
            )
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
    result = subprocess.run(
        ["systemctl", "start", settings.ollama_service],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        error(
            f"Failed to restart {settings.ollama_service}. Start it manually."
        )
        return False
    return True


@dataclass
class Session:
    """Holds lock, ownership, temps, and optional service restart."""

    settings: Settings
    uid: int
    gid: int
    lock_fd: int
    service_was_active: bool = False
    temps: list[Path] = field(default_factory=list)
    skip_privileges: bool = False

    def register_temp(self, path: Path) -> Path:
        """Track a temporary path for cleanup."""
        self.temps.append(path)
        return path

    def chown_path(self, path: Path) -> None:
        """Apply Ollama ownership unless privileges are skipped."""
        if self.skip_privileges:
            return
        os.chown(path, self.uid, self.gid)

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
        self.close(success=exc_type is None)


def prepare_read_operation(
    settings: Settings,
    *,
    argv: Sequence[str] | None = None,
    skip_privileges: bool = False,
) -> Session:
    """Validate stores and acquire lock for read operations."""
    if skip_privileges:
        validate_roots(settings)
        ensure_hot_store(settings)
        for path in (
            settings.cold_root,
            settings.cold_root / "manifests",
            settings.cold_root / "blobs",
        ):
            path.mkdir(mode=0o755, parents=True, exist_ok=True)
        try:
            uid, gid = ollama_ids(settings)
        except StoreUnsafeError:
            uid, gid = os.getuid(), os.getgid()
        return Session(
            settings=settings,
            uid=uid,
            gid=gid,
            lock_fd=-1,
            skip_privileges=True,
        )

    require_root(argv)
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
    uid, gid = ensure_cold_store(settings)
    lock_fd = acquire_lock(settings.lock_file)
    return Session(
        settings=settings,
        uid=uid,
        gid=gid,
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
        settings, argv=argv, skip_privileges=skip_privileges
    )
    if not skip_privileges:
        require_commands("rsync", "df", "pgrep", "sync")
    if stop_service and not skip_privileges:
        session.service_was_active = stop_ollama_for_mutation(settings)
    return session
