"""Privileges, locking, mounts, and Ollama service lifecycle."""

from __future__ import annotations

import errno
import fcntl
import os
import shutil
import subprocess
import sys
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


def iter_store_dirs(root: Path) -> list[Path]:
    """List store root, manifests, blobs, and nested dirs under them.

    Nested host/namespace/model dirs under ``manifests/`` are included so
    mutations fail before transfers when only leaf dirs lack group write.
    Does not follow directory symlinks.
    """
    manifests = root / "manifests"
    blobs = root / "blobs"
    ordered: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        key = path
        if key in seen:
            return
        seen.add(key)
        ordered.append(path)

    add(root)
    add(manifests)
    add(blobs)
    for base in (manifests, blobs):
        if not base.is_dir() or base.is_symlink():
            continue
        for dirpath, dirnames, _filenames in os.walk(base, followlinks=False):
            current = Path(dirpath)
            # Do not descend through symlinked directories.
            dirnames[:] = [
                name
                for name in dirnames
                if not (current / name).is_symlink()
            ]
            if current != base:
                add(current)
    return ordered


def inaccessible_paths(paths: Sequence[Path], mode: int) -> list[Path]:
    """Return every path that fails ``os.access`` for ``mode``."""
    return [path for path in paths if not os.access(path, mode)]


def prompt_yes_no(question: str, *, input_func: Callable[[str], str] | None = None) -> bool:
    """Ask a yes/no question on a TTY; return False when non-interactive."""
    if input_func is None and not sys.stdin.isatty():
        return False
    reader = input_func or input
    try:
        answer = reader(f"{question} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _run_privileged(
    argv_as_root: Sequence[str],
    *,
    failure: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> None:
    """Run ``argv_as_root`` as root, prefixing ``sudo`` when needed."""
    if os.geteuid() == 0:
        argv = list(argv_as_root)
    else:
        sudo = shutil.which("sudo")
        if sudo is None:
            raise StoreUnsafeError(
                "sudo is required to fix store directory permissions. "
                "Install sudo, or run as root."
            )
        argv = [sudo, *argv_as_root]
    run = runner or (
        lambda *args, **kwargs: subprocess.run(
            *args,
            text=True,
            capture_output=True,
            check=False,
            **kwargs,
        )
    )
    result = run(argv)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise StoreUnsafeError(
            failure + (f": {detail}" if detail else ".")
        )


def chmod_group_write(
    paths: Sequence[Path],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> None:
    """Run ``chmod g+w`` on paths, via ``sudo`` when not root."""
    if not paths:
        return
    _run_privileged(
        ["chmod", "g+w", "--", *[str(p) for p in paths]],
        failure="Failed to apply group-write permissions",
        runner=runner,
    )


def validate_chown_owner(owner: str) -> str:
    """Return a safe owner name for ``chown owner:owner``, or raise."""
    name = owner.strip()
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or ":" in name
        or "\0" in name
        or any(ch.isspace() for ch in name)
    ):
        raise StoreUnsafeError(
            f"Invalid ollama_user for ownership fix: {owner!r}"
        )
    return name


def chown_owner(
    paths: Sequence[Path],
    owner: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> None:
    """Run ``chown owner:owner`` on paths, via ``sudo`` when not root.

    Only used after an interactive confirmation when group-write alone cannot
    restore access (e.g. directories owned by root:root).
    """
    if not paths:
        return
    name = validate_chown_owner(owner)
    _run_privileged(
        ["chown", f"{name}:{name}", "--", *[str(p) for p in paths]],
        failure=f"Failed to change ownership to {name}:{name}",
        runner=runner,
    )


def ensure_store_access(
    label: str,
    root: Path,
    *,
    writable: bool,
    offer_fix: bool = False,
    owner: str | None = None,
    prompt: Callable[[str], bool] | None = None,
    chmod: Callable[[Sequence[Path]], None] | None = None,
    chown: Callable[[Sequence[Path], str], None] | None = None,
) -> None:
    """Require access to ``root`` and nested store dirs; optionally fix writes.

    Collects every inaccessible directory before reporting. When ``writable``
    and ``offer_fix`` are true, prompts to run ``sudo chmod g+w``, then if
    paths remain inaccessible and ``owner`` is set, prompts for
    ``sudo chown owner:owner`` (and reapplies group-write).
    """
    mode = os.R_OK | os.X_OK | (os.W_OK if writable else 0)
    need = "read/write" if writable else "read"
    paths = iter_store_dirs(root)
    bad = inaccessible_paths(paths, mode)
    if not bad:
        return

    listing = "\n".join(f"  {path}" for path in bad)
    ask = prompt if prompt is not None else prompt_yes_no
    apply_chmod = chmod if chmod is not None else chmod_group_write
    apply_chown = chown if chown is not None else chown_owner

    if writable and offer_fix:
        warn(
            f"Cannot {need} {len(bad)} {label} store path(s):\n{listing}"
        )
        interactive = prompt is not None or sys.stdin.isatty()
        if not interactive:
            raise StoreUnsafeError(
                f"Cannot {need} {len(bad)} {label} store path(s):\n"
                f"{listing}\n"
                "Fix permissions and retry (non-interactive; no fix prompt)."
            )

        chmod_ok = ask(
            "Grant group-write (sudo chmod g+w) on these directories?"
        )
        if chmod_ok:
            info(f"Applying group-write to {len(bad)} {label} path(s)...")
            apply_chmod(bad)
            still_bad = inaccessible_paths(paths, mode)
            if not still_bad:
                info(f"{label.capitalize()} store permissions look OK.")
                return
            bad = still_bad
            listing = "\n".join(f"  {path}" for path in bad)
            warn(
                f"Still cannot {need} {len(bad)} {label} store path(s) "
                f"after chmod (often wrong group/owner):\n{listing}"
            )
        elif owner is None:
            raise StoreUnsafeError(
                f"Cannot {need} {len(bad)} {label} store path(s):\n"
                f"{listing}\n"
                "Permission fix declined. Fix permissions and retry."
            )

        if owner is not None:
            try:
                owner_name = validate_chown_owner(owner)
            except StoreUnsafeError:
                owner_name = owner.strip() or owner
            if ask(
                f"Change ownership to {owner_name}:{owner_name} "
                f"(sudo chown) on remaining directories?"
            ):
                info(
                    f"Changing ownership of {len(bad)} {label} path(s) "
                    f"to {owner_name}:{owner_name}..."
                )
                apply_chown(bad, owner)
                # Group write may still be missing after a chown from 755.
                apply_chmod(bad)
                still_bad = inaccessible_paths(paths, mode)
                if not still_bad:
                    info(f"{label.capitalize()} store permissions look OK.")
                    return
                still = "\n".join(f"  {path}" for path in still_bad)
                raise StoreUnsafeError(
                    f"Still cannot {need} {len(still_bad)} {label} store "
                    f"path(s) after chown/chmod:\n{still}\n"
                    "Fix permissions manually and retry."
                )
            raise StoreUnsafeError(
                f"Cannot {need} {len(bad)} {label} store path(s):\n"
                f"{listing}\n"
                "Permission fix declined. Fix permissions and retry."
            )

        still = "\n".join(f"  {path}" for path in bad)
        raise StoreUnsafeError(
            f"Still cannot {need} {len(bad)} {label} store "
            f"path(s) after chmod:\n{still}\n"
            "Fix permissions manually and retry "
            "(or set ollama_user and allow chown)."
        )

    raise StoreUnsafeError(
        f"Cannot {need} {len(bad)} {label} store path(s):\n{listing}\n"
        "Fix permissions and retry."
    )


def ensure_hot_store(
    settings: Settings,
    *,
    writable: bool = False,
    offer_fix: bool = False,
) -> None:
    """Require a complete, accessible hot store (no symlink dirs).

    When ``writable`` is true (mutations), also require write access so
    freeze/thaw fail before long transfers. Optional interactive fixes may
    chmod / chown after confirmation.
    """
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
    ensure_store_access(
        "hot",
        hot,
        writable=writable,
        offer_fix=offer_fix,
        owner=settings.ollama_user if offer_fix else None,
    )


def ensure_cold_store(
    settings: Settings,
    *,
    writable: bool = True,
    offer_fix: bool = False,
) -> None:
    """Ensure cold store directories exist and are accessible.

    Permission problems are reported so the user can fix them (optionally
    via confirmed ``sudo chmod g+w`` and ``sudo chown`` to ``ollama_user``).
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
    ensure_store_access(
        "cold",
        cold,
        writable=writable,
        offer_fix=offer_fix,
        owner=settings.ollama_user if offer_fix else None,
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
    hot_writable: bool = False,
) -> Session:
    """Validate stores and acquire lock for read operations.

    Does not elevate the process. Store paths must be readable (and writable
    when requested) by the invoking user.
    """
    del argv  # Kept for call-site compatibility; no longer used for re-exec.
    offer_fix = not skip_privileges
    if skip_privileges:
        validate_roots(settings)
        ensure_hot_store(
            settings,
            writable=hot_writable,
            offer_fix=False,
        )
        ensure_cold_store(
            settings,
            writable=True,
            offer_fix=False,
        )
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
    ensure_hot_store(
        settings,
        writable=hot_writable,
        offer_fix=offer_fix and hot_writable,
    )
    ensure_cold_store(
        settings,
        writable=cold_writable,
        offer_fix=offer_fix and cold_writable,
    )
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
        hot_writable=True,
    )
    if not skip_privileges:
        require_commands("rsync", "df", "pgrep", "sync")
    if stop_service and not skip_privileges:
        session.service_was_active = stop_ollama_for_mutation(settings)
    return session
