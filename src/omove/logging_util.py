"""Logging helpers matching Bash omove tone."""

from __future__ import annotations

import sys


def log(message: str) -> None:
    """Print a plain message to stdout."""
    print(message)


def info(message: str) -> None:
    """Print an INFO-prefixed message to stdout."""
    print(f"INFO: {message}")


def warn(message: str) -> None:
    """Print a WARNING-prefixed message to stderr."""
    print(f"WARNING: {message}", file=sys.stderr)


def error(message: str) -> None:
    """Print an ERROR-prefixed message to stderr."""
    print(f"ERROR: {message}", file=sys.stderr)
