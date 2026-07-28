"""Manifest path validation, canonicalization, and query matching."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from omove.config import DEFAULT_HOST, DEFAULT_NAMESPACE, DEFAULT_TAG

_HOST_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,349}$")
_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,79}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}$")


class LayoutKind(str, Enum):
    """On-disk manifest layout kind."""

    CANONICAL = "canonical"
    LEGACY_COLD = "legacy-cold"
    LEGACY_FLAT = "legacy-flat"


@dataclass(frozen=True)
class CanonicalPath:
    """Canonicalized manifest relative path."""

    rel: str
    host: str
    namespace: str
    model: str
    tag: str
    layout: LayoutKind

    @property
    def parts(self) -> tuple[str, str, str, str]:
        return self.host, self.namespace, self.model, self.tag


def valid_manifest_relpath(rel: str) -> bool:
    """Return True if *rel* is host/namespace/model/tag."""
    parts = rel.split("/")
    if len(parts) != 4:
        return False
    host, namespace, model, tag = parts
    return bool(
        host
        and namespace
        and model
        and tag
        and _HOST_RE.fullmatch(host)
        and _NAMESPACE_RE.fullmatch(namespace)
        and _MODEL_RE.fullmatch(model)
        and _TAG_RE.fullmatch(tag)
    )


def valid_legacy_manifest_relpath(rel: str) -> bool:
    """Return True if *rel* is legacy model/tag."""
    parts = rel.split("/")
    if len(parts) != 2:
        return False
    model, tag = parts
    return bool(
        model and tag and _MODEL_RE.fullmatch(model) and _TAG_RE.fullmatch(tag)
    )


def valid_flat_tag_manifest_relpath(rel: str) -> bool:
    """Return True if *rel* is host/namespace/model:tag."""
    parts = rel.split("/")
    if len(parts) != 3:
        return False
    host, namespace, model_tag = parts
    if ":" not in model_tag:
        return False
    model, tag = model_tag.rsplit(":", 1)
    return bool(
        host
        and namespace
        and model
        and tag
        and _HOST_RE.fullmatch(host)
        and _NAMESPACE_RE.fullmatch(namespace)
        and _MODEL_RE.fullmatch(model)
        and _TAG_RE.fullmatch(tag)
    )


def canonicalize_manifest_relpath(
    rel: str,
    *,
    default_host: str = DEFAULT_HOST,
    default_namespace: str = DEFAULT_NAMESPACE,
) -> CanonicalPath | None:
    """Canonicalize a manifest relative path, or None if invalid."""
    if valid_manifest_relpath(rel):
        host, namespace, model, tag = rel.split("/")
        return CanonicalPath(
            rel=rel,
            host=host,
            namespace=namespace,
            model=model,
            tag=tag,
            layout=LayoutKind.CANONICAL,
        )
    if valid_legacy_manifest_relpath(rel):
        model, tag = rel.split("/")
        canonical = f"{default_host}/{default_namespace}/{model}/{tag}"
        return CanonicalPath(
            rel=canonical,
            host=default_host,
            namespace=default_namespace,
            model=model,
            tag=tag,
            layout=LayoutKind.LEGACY_COLD,
        )
    if valid_flat_tag_manifest_relpath(rel):
        host, namespace, model_tag = rel.split("/")
        model, tag = model_tag.rsplit(":", 1)
        canonical = f"{host}/{namespace}/{model}/{tag}"
        return CanonicalPath(
            rel=canonical,
            host=host,
            namespace=namespace,
            model=model,
            tag=tag,
            layout=LayoutKind.LEGACY_FLAT,
        )
    return None


def legacy_relpath_for_canonical(
    rel: str,
    *,
    default_host: str = DEFAULT_HOST,
    default_namespace: str = DEFAULT_NAMESPACE,
) -> str | None:
    """Return model/tag legacy path for a default-host canonical rel."""
    if not valid_manifest_relpath(rel):
        return None
    host, namespace, model, tag = rel.split("/")
    if host != default_host or namespace != default_namespace:
        return None
    return f"{model}/{tag}"


def manifest_display_name(
    rel: str,
    *,
    default_host: str = DEFAULT_HOST,
    default_namespace: str = DEFAULT_NAMESPACE,
) -> str | None:
    """Human-readable model name for a relative manifest path."""
    canonical = canonicalize_manifest_relpath(
        rel, default_host=default_host, default_namespace=default_namespace
    )
    if canonical is None:
        return None
    host, namespace, model, tag = canonical.parts
    if host == default_host and namespace == default_namespace:
        return f"{model}:{tag}"
    if host == default_host:
        return f"{namespace}/{model}:{tag}"
    return f"{host}/{namespace}/{model}:{tag}"


def query_matches_relpath(
    query: str,
    rel: str,
    *,
    default_host: str = DEFAULT_HOST,
    default_namespace: str = DEFAULT_NAMESPACE,
    default_tag: str = DEFAULT_TAG,
) -> bool:
    """Return True if *query* refers to the model at *rel*."""
    canonical = canonicalize_manifest_relpath(
        rel, default_host=default_host, default_namespace=default_namespace
    )
    if canonical is None:
        return False
    host, namespace, model, tag = canonical.parts
    full = f"{host}/{namespace}/{model}:{tag}"
    namespace_name = f"{namespace}/{model}:{tag}"
    short = f"{model}:{tag}"

    if query in {rel, canonical.rel, full}:
        return True

    if host == default_host:
        if query == namespace_name:
            return True
        if namespace == default_namespace and query == short:
            return True

    if tag == default_tag:
        if query == f"{host}/{namespace}/{model}":
            return True
        if host == default_host:
            if query == f"{namespace}/{model}":
                return True
            if namespace == default_namespace and query == model:
                return True

    return False
