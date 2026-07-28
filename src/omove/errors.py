"""Typed errors for omove operations."""

from __future__ import annotations


class OmoveError(Exception):
    """Base error for omove operations."""

    exit_code: int = 1


class UsageError(OmoveError):
    """Invalid CLI usage."""

    exit_code = 2


class StoreUnsafeError(OmoveError):
    """Store layout or mount checks failed."""


class ModelNotFoundError(OmoveError):
    """No model matched the query."""


class AmbiguousModelError(OmoveError):
    """Multiple models matched the query."""

    def __init__(self, query: str, matches: list[str]) -> None:
        self.query = query
        self.matches = matches
        names = "\n".join(f"  {name}" for name in matches)
        super().__init__(
            f"Model name is ambiguous: {query}\nMatching models:\n{names}"
        )


class InsufficientSpaceError(OmoveError):
    """Destination filesystem lacks free space."""


class ManifestError(OmoveError):
    """Manifest is missing or invalid."""
