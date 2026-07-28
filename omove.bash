#!/usr/bin/env bash
# omove - Ollama Tiered Storage Manager
# v3.2.0: add safe migration from legacy omove layouts.
# Safely transitions Ollama models between hot and cold storage while
# preserving canonical manifest paths and shared, content-addressed blobs.
#
# DEPRECATED: Prefer the Python implementation:
#   PYTHONPATH=src python3 -m omove ...
# or, after packaging:  pipx install . && omove ...
# This Bash script is kept temporarily as a behavioral parity reference.

if [[ -z "${OMOVE_SUPPRESS_DEPRECATION:-}" ]]; then
    printf 'WARNING: Bash omove is deprecated; use: PYTHONPATH=src python3 -m omove\n' >&2
fi

set -Eeuo pipefail
IFS=$'\n\t'
umask 022

VERSION="3.2.0"

HOT_ROOT="${OMOVE_HOT_PATH:-${OLLAMA_MODELS:-/usr/share/ollama/.ollama/models}}"
COLD_ROOT="${OMOVE_COLD_PATH:-/media/elysium/ollama_archive}"
COLD_MOUNT="${OMOVE_COLD_MOUNT:-$(dirname -- "$COLD_ROOT")}"
OLLAMA_USER="${OMOVE_OLLAMA_USER:-ollama}"
OLLAMA_SERVICE="${OMOVE_OLLAMA_SERVICE:-ollama.service}"
LOCK_FILE="${OMOVE_LOCK_FILE:-/run/lock/omove.lock}"
ALLOW_UNMOUNTED_COLD="${OMOVE_ALLOW_UNMOUNTED_COLD:-0}"
ALLOW_LIVE_OLLAMA="${OMOVE_ALLOW_LIVE_OLLAMA:-0}"

DEFAULT_HOST="registry.ollama.ai"
DEFAULT_NAMESPACE="library"
DEFAULT_TAG="latest"

HOT_ROOT="$(readlink -m -- "$HOT_ROOT")"
COLD_ROOT="$(readlink -m -- "$COLD_ROOT")"
COLD_MOUNT="$(readlink -m -- "$COLD_MOUNT")"
LOCK_FILE="$(readlink -m -- "$LOCK_FILE")"

OLLAMA_GROUP=""
SERVICE_WAS_ACTIVE=0
LOCK_FD=""
TEMP_PATHS=()
declare -A VERIFIED_BLOBS=()
MANIFEST_DIGESTS=()
MANIFEST_LOGICAL_SIZE=0
RESOLVED_REL=""
RESOLVED_CANONICAL_REL=""
CANONICAL_REL=""
GC_RECLAIMED=0
LAYOUT_KIND=""
MIGRATED_REL=""
MIGRATED_CHANGED=0
MIGRATION_MANIFESTS=0
MIGRATION_BLOBS=0
MIGRATION_UNRESOLVED=0
ORIGINAL_ARGS=("$@")

log()   { printf '%s\n' "$*"; }
info()  { printf 'INFO: %s\n' "$*"; }
warn()  { printf 'WARNING: %s\n' "$*" >&2; }
error() { printf 'ERROR: %s\n' "$*" >&2; }

usage() {
    cat <<'USAGE'
omove - Ollama Tiered Storage Manager

Usage:
  omove list [cold|hot]
  omove freeze <model> [model ...]
  omove thaw <model> [model ...]
  omove verify [cold|hot] [model ...]
  omove migrate [all|cold|hot]
  omove migrate cold <model> [model ...]
  omove migrate hot <model> [model ...]
  omove version
  omove help

Model names may be supplied in normal Ollama forms, for example:
  llama3.2
  llama3.2:latest
  team/model:production
  registry.example.com:5000/team/model:production

Environment overrides:
  OLLAMA_MODELS                 Effective Ollama model root
  OMOVE_HOT_PATH                Overrides OLLAMA_MODELS
  OMOVE_COLD_PATH               Cold archive root
  OMOVE_COLD_MOUNT              Mount point that must contain cold storage
  OMOVE_OLLAMA_USER             Ollama service account, default: ollama
  OMOVE_OLLAMA_SERVICE          systemd unit, default: ollama.service
  OMOVE_LOCK_FILE               Lock file, default: /run/lock/omove.lock
  OMOVE_ALLOW_UNMOUNTED_COLD=1  Permit cold storage on a non-mount-point path
  OMOVE_ALLOW_LIVE_OLLAMA=1     Permit mutation while an Ollama process is live

The freeze, thaw, and migrate commands stop an active systemd Ollama service
and restart it when the operation finishes. A manually started Ollama process causes the
operation to abort unless OMOVE_ALLOW_LIVE_OLLAMA=1 is explicitly set.
USAGE
}

cleanup() {
    local rc=$?
    trap - EXIT INT TERM HUP

    local path
    for path in "${TEMP_PATHS[@]:-}"; do
        [[ -n "$path" ]] || continue
        if [[ -d "$path" ]]; then
            rm -rf -- "$path" 2>/dev/null || true
        else
            rm -f -- "$path" 2>/dev/null || true
        fi
    done

    if (( SERVICE_WAS_ACTIVE == 1 )); then
        if ! systemctl start "$OLLAMA_SERVICE"; then
            error "Failed to restart $OLLAMA_SERVICE. Start it manually."
            (( rc == 0 )) && rc=1
        fi
    fi

    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

register_temp() {
    TEMP_PATHS+=("$1")
}

require_root() {
    if (( EUID == 0 )); then
        return 0
    fi

    command -v sudo >/dev/null 2>&1 || {
        error "This command requires root privileges and sudo is unavailable."
        exit 1
    }

    exec sudo --preserve-env=OLLAMA_MODELS,OMOVE_HOT_PATH,OMOVE_COLD_PATH,OMOVE_COLD_MOUNT,OMOVE_OLLAMA_USER,OMOVE_OLLAMA_SERVICE,OMOVE_LOCK_FILE,OMOVE_ALLOW_UNMOUNTED_COLD,OMOVE_ALLOW_LIVE_OLLAMA -- "$0" "${ORIGINAL_ARGS[@]}"
}

require_commands() {
    local missing=0
    local command_name
    for command_name in "$@"; do
        if ! command -v "$command_name" >/dev/null 2>&1; then
            error "Required command not found: $command_name"
            missing=1
        fi
    done
    (( missing == 0 ))
}

validate_roots() {
    [[ "$HOT_ROOT" != "/" ]] || { error "Hot root cannot be /."; return 1; }
    [[ "$COLD_ROOT" != "/" ]] || { error "Cold root cannot be /."; return 1; }
    [[ "$HOT_ROOT" != "$COLD_ROOT" ]] || {
        error "Hot and cold roots resolve to the same directory: $HOT_ROOT"
        return 1
    }

    case "$HOT_ROOT/" in
        "$COLD_ROOT/"*)
            error "Hot storage cannot be inside cold storage."
            return 1
            ;;
    esac
    case "$COLD_ROOT/" in
        "$HOT_ROOT/"*)
            error "Cold storage cannot be inside hot storage."
            return 1
            ;;
    esac
}

acquire_lock() {
    install -d -m 0755 -- "$(dirname -- "$LOCK_FILE")"
    exec {LOCK_FD}>"$LOCK_FILE"
    if ! flock -x "$LOCK_FD"; then
        error "Unable to acquire lock: $LOCK_FILE"
        return 1
    fi
}

validate_cold_mount() {
    if [[ "$ALLOW_UNMOUNTED_COLD" == "1" ]]; then
        return 0
    fi

    if [[ ! -d "$COLD_MOUNT" ]]; then
        error "Cold mount path does not exist: $COLD_MOUNT"
        return 1
    fi

    if ! mountpoint -q -- "$COLD_MOUNT"; then
        error "$COLD_MOUNT is not a mount point. Refusing to use $COLD_ROOT."
        error "Set OMOVE_COLD_MOUNT correctly or explicitly set OMOVE_ALLOW_UNMOUNTED_COLD=1."
        return 1
    fi
}

ensure_hot_store() {
    if [[ ! -d "$HOT_ROOT/manifests" || ! -d "$HOT_ROOT/blobs" ]]; then
        error "Hot Ollama model store is incomplete or missing: $HOT_ROOT"
        return 1
    fi
    [[ ! -L "$HOT_ROOT/manifests" && ! -L "$HOT_ROOT/blobs" ]] || {
        error "Hot manifests or blobs directory is a symbolic link. Refusing to continue."
        return 1
    }
}

ensure_cold_store() {
    validate_cold_mount || return 1

    if ! id "$OLLAMA_USER" >/dev/null 2>&1; then
        error "Ollama user does not exist: $OLLAMA_USER"
        return 1
    fi
    OLLAMA_GROUP="$(id -gn "$OLLAMA_USER")"

    install -d -o "$OLLAMA_USER" -g "$OLLAMA_GROUP" -m 0755 -- \
        "$COLD_ROOT" "$COLD_ROOT/manifests" "$COLD_ROOT/blobs"

    [[ ! -L "$COLD_ROOT/manifests" && ! -L "$COLD_ROOT/blobs" ]] || {
        error "Cold manifests or blobs directory is a symbolic link. Refusing to continue."
        return 1
    }
}

prepare_read_operation() {
    require_root
    require_commands readlink flock find sort jq sha256sum stat numfmt date mountpoint install || exit 1
    validate_roots || exit 1
    acquire_lock || exit 1
    ensure_hot_store || exit 1
    ensure_cold_store || exit 1
}

stop_ollama_for_mutation() {
    if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$OLLAMA_SERVICE" 2>/dev/null; then
        info "Stopping $OLLAMA_SERVICE for a consistent storage transaction."
        systemctl stop "$OLLAMA_SERVICE" || {
            error "Failed to stop $OLLAMA_SERVICE."
            return 1
        }
        SERVICE_WAS_ACTIVE=1
    fi

    if pgrep -x ollama >/dev/null 2>&1; then
        if [[ "$ALLOW_LIVE_OLLAMA" != "1" ]]; then
            error "An Ollama process is still running. Refusing to modify its model store."
            error "Stop it first, or explicitly set OMOVE_ALLOW_LIVE_OLLAMA=1."
            return 1
        fi
        warn "Proceeding while Ollama is running because OMOVE_ALLOW_LIVE_OLLAMA=1."
    fi
}

prepare_mutation() {
    prepare_read_operation "$@"
    require_commands rsync df pgrep sync || exit 1
    stop_ollama_for_mutation || exit 1
}

valid_manifest_relpath() {
    local rel=$1
    local host namespace model tag extra
    IFS='/' read -r host namespace model tag extra <<< "$rel"

    [[ -n "$host" && -n "$namespace" && -n "$model" && -n "$tag" && -z "${extra:-}" ]] || return 1
    [[ "$host" =~ ^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,349}$ ]] || return 1
    [[ "$namespace" =~ ^[A-Za-z0-9_][A-Za-z0-9_-]{0,79}$ ]] || return 1
    [[ "$model" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}$ ]] || return 1
    [[ "$tag" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}$ ]] || return 1
}

valid_legacy_manifest_relpath() {
    local rel=$1
    local model tag extra
    IFS='/' read -r model tag extra <<< "$rel"

    [[ -n "$model" && -n "$tag" && -z "${extra:-}" ]] || return 1
    [[ "$model" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}$ ]] || return 1
    [[ "$tag" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}$ ]] || return 1
}

# Older omove thaw releases created hot manifests as
# host/namespace/model:tag instead of host/namespace/model/tag.
valid_flat_tag_manifest_relpath() {
    local rel=$1
    local host namespace model_tag extra model tag
    IFS='/' read -r host namespace model_tag extra <<< "$rel"

    [[ -n "$host" && -n "$namespace" && -n "$model_tag" && -z "${extra:-}" ]] || return 1
    [[ "$model_tag" == *:* ]] || return 1
    model="${model_tag%:*}"
    tag="${model_tag##*:}"

    [[ "$host" =~ ^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,349}$ ]] || return 1
    [[ "$namespace" =~ ^[A-Za-z0-9_][A-Za-z0-9_-]{0,79}$ ]] || return 1
    [[ "$model" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}$ ]] || return 1
    [[ "$tag" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}$ ]] || return 1
}

canonicalize_manifest_relpath() {
    local rel=$1
    local host namespace model_tag model tag

    CANONICAL_REL=""
    LAYOUT_KIND=""
    if valid_manifest_relpath "$rel"; then
        CANONICAL_REL="$rel"
        LAYOUT_KIND="canonical"
        return 0
    fi

    if valid_legacy_manifest_relpath "$rel"; then
        IFS='/' read -r model tag <<< "$rel"
        CANONICAL_REL="$DEFAULT_HOST/$DEFAULT_NAMESPACE/$model/$tag"
        LAYOUT_KIND="legacy-cold"
        return 0
    fi

    if valid_flat_tag_manifest_relpath "$rel"; then
        IFS='/' read -r host namespace model_tag <<< "$rel"
        model="${model_tag%:*}"
        tag="${model_tag##*:}"
        CANONICAL_REL="$host/$namespace/$model/$tag"
        LAYOUT_KIND="legacy-flat"
        return 0
    fi

    return 1
}

legacy_relpath_for_canonical() {
    local rel=$1
    local host namespace model tag

    valid_manifest_relpath "$rel" || return 1
    IFS='/' read -r host namespace model tag <<< "$rel"
    [[ "$host" == "$DEFAULT_HOST" && "$namespace" == "$DEFAULT_NAMESPACE" ]] || return 1
    printf '%s/%s\n' "$model" "$tag"
}

manifest_display_name() {
    local rel=$1
    local host namespace model tag

    canonicalize_manifest_relpath "$rel" || return 1
    IFS='/' read -r host namespace model tag <<< "$CANONICAL_REL"

    if [[ "$host" == "$DEFAULT_HOST" && "$namespace" == "$DEFAULT_NAMESPACE" ]]; then
        printf '%s:%s\n' "$model" "$tag"
    elif [[ "$host" == "$DEFAULT_HOST" ]]; then
        printf '%s/%s:%s\n' "$namespace" "$model" "$tag"
    else
        printf '%s/%s/%s:%s\n' "$host" "$namespace" "$model" "$tag"
    fi
}

query_matches_relpath() {
    local query=$1
    local rel=$2
    local host namespace model tag

    canonicalize_manifest_relpath "$rel" || return 1
    IFS='/' read -r host namespace model tag <<< "$CANONICAL_REL"

    local full="$host/$namespace/$model:$tag"
    local namespace_name="$namespace/$model:$tag"
    local short="$model:$tag"

    [[ "$query" == "$rel" || "$query" == "$CANONICAL_REL" || "$query" == "$full" ]] && return 0

    if [[ "$host" == "$DEFAULT_HOST" ]]; then
        [[ "$query" == "$namespace_name" ]] && return 0
        if [[ "$namespace" == "$DEFAULT_NAMESPACE" && "$query" == "$short" ]]; then
            return 0
        fi
    fi

    if [[ "$tag" == "$DEFAULT_TAG" ]]; then
        [[ "$query" == "$host/$namespace/$model" ]] && return 0
        if [[ "$host" == "$DEFAULT_HOST" ]]; then
            [[ "$query" == "$namespace/$model" ]] && return 0
            if [[ "$namespace" == "$DEFAULT_NAMESPACE" && "$query" == "$model" ]]; then
                return 0
            fi
        fi
    fi

    return 1
}

resolve_manifest() {
    local root=$1
    local query=$2
    local manifest_root="$root/manifests"
    local rel
    local -a candidates=()

    RESOLVED_REL=""
    RESOLVED_CANONICAL_REL=""
    [[ -d "$manifest_root" ]] || {
        error "Manifest directory does not exist: $manifest_root"
        return 1
    }

    while IFS= read -r -d '' rel; do
        canonicalize_manifest_relpath "$rel" || continue
        if query_matches_relpath "$query" "$rel"; then
            candidates+=("$rel")
        fi
    done < <(find "$manifest_root" -mindepth 2 -maxdepth 4 -type f -printf '%P\0' | sort -z)

    if (( ${#candidates[@]} == 0 )); then
        error "Model not found in $(basename -- "$root") storage: $query"
        return 1
    fi

    if (( ${#candidates[@]} > 1 )); then
        error "Model name is ambiguous: $query"
        printf 'Matching models:\n' >&2
        for rel in "${candidates[@]}"; do
            printf '  %s\n' "$(manifest_display_name "$rel")" >&2
        done
        return 1
    fi

    RESOLVED_REL="${candidates[0]}"
    canonicalize_manifest_relpath "$RESOLVED_REL" || return 1
    RESOLVED_CANONICAL_REL="$CANONICAL_REL"
}

load_manifest() {
    local manifest=$1
    local digest
    local raw_digests
    local deduplicated

    MANIFEST_DIGESTS=()
    MANIFEST_LOGICAL_SIZE=0

    [[ -f "$manifest" && ! -L "$manifest" ]] || {
        error "Manifest is not a regular file: $manifest"
        return 1
    }

    if ! jq -e '
        (.schemaVersion == 2) and
        (.config | type == "object") and
        (.config.digest | type == "string") and
        (.layers | type == "array")
    ' "$manifest" >/dev/null; then
        error "Invalid Ollama manifest: $manifest"
        return 1
    fi

    raw_digests="$(jq -r '.config.digest, (.layers[]? | .digest)' "$manifest")" || {
        error "Unable to read digests from manifest: $manifest"
        return 1
    }

    while IFS= read -r digest; do
        [[ "$digest" =~ ^sha256:[0-9a-fA-F]{64}$ ]] || {
            error "Invalid blob digest in $manifest: $digest"
            return 1
        }
        MANIFEST_DIGESTS+=("${digest,,}")
    done <<< "$raw_digests"

    (( ${#MANIFEST_DIGESTS[@]} > 0 )) || {
        error "Manifest contains no blob digests: $manifest"
        return 1
    }

    deduplicated="$(printf '%s\n' "${MANIFEST_DIGESTS[@]}" | sort -u)"
    mapfile -t MANIFEST_DIGESTS <<< "$deduplicated"

    MANIFEST_LOGICAL_SIZE="$(jq -r '
        ((.config.size // 0) + ([.layers[]?.size // 0] | add // 0))
    ' "$manifest")" || return 1

    [[ "$MANIFEST_LOGICAL_SIZE" =~ ^[0-9]+$ ]] || {
        error "Manifest contains invalid size metadata: $manifest"
        return 1
    }
}

blob_filename() {
    local digest=$1
    printf 'sha256-%s\n' "${digest#sha256:}"
}

verify_blob() {
    local path=$1
    local digest=$2
    local cache_key="$path|$digest"
    local output actual expected

    if [[ -n "${VERIFIED_BLOBS[$cache_key]:-}" ]]; then
        return 0
    fi

    [[ -f "$path" && ! -L "$path" ]] || {
        error "Missing blob: $path"
        return 1
    }

    output="$(sha256sum -- "$path")" || {
        error "Unable to hash blob: $path"
        return 1
    }
    actual="${output%% *}"
    expected="${digest#sha256:}"

    if [[ "${actual,,}" != "${expected,,}" ]]; then
        error "Blob checksum mismatch: $path"
        error "Expected $expected but found $actual"
        return 1
    fi

    VERIFIED_BLOBS[$cache_key]=1
}

verify_manifest_blobs() {
    local root=$1
    local manifest=$2
    local digest filename

    load_manifest "$manifest" || return 1
    for digest in "${MANIFEST_DIGESTS[@]}"; do
        filename="$(blob_filename "$digest")"
        verify_blob "$root/blobs/$filename" "$digest" || return 1
    done
}

available_bytes() {
    local path=$1
    local value
    value="$(df -B1 --output=avail "$path" | tail -n 1 | tr -d '[:space:]')" || return 1
    [[ "$value" =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "$value"
}

check_destination_space() {
    local source_root=$1
    local destination_root=$2
    shift 2
    local -a digests=("$@")
    local digest filename source_blob destination_blob size
    local required=0
    local available reserve needed

    for digest in "${digests[@]}"; do
        filename="$(blob_filename "$digest")"
        source_blob="$source_root/blobs/$filename"
        destination_blob="$destination_root/blobs/$filename"

        if [[ -e "$destination_blob" ]]; then
            verify_blob "$destination_blob" "$digest" || return 1
            continue
        fi

        size="$(stat -c '%s' -- "$source_blob")" || return 1
        required=$((required + size))
    done

    available="$(available_bytes "$destination_root/blobs")" || {
        error "Unable to determine free space for $destination_root"
        return 1
    }

    reserve=$((64 * 1024 * 1024))
    if (( required / 20 > reserve )); then
        reserve=$((required / 20))
    fi

    if (( required == 0 )); then
        needed=$((1024 * 1024))
    else
        needed=$((required + reserve))
    fi

    if (( available < needed )); then
        error "Insufficient free space in $destination_root"
        error "Need approximately $(numfmt --to=iec-i --suffix=B "$needed"), available $(numfmt --to=iec-i --suffix=B "$available")."
        return 1
    fi
}

copy_blob_verified() {
    local source_root=$1
    local destination_root=$2
    local digest=$3
    local filename source_blob destination_blob temp_blob
    local -a rsync_options=(-a --sparse --protect-args)

    filename="$(blob_filename "$digest")"
    source_blob="$source_root/blobs/$filename"
    destination_blob="$destination_root/blobs/$filename"

    verify_blob "$source_blob" "$digest" || return 1

    if [[ -e "$destination_blob" ]]; then
        verify_blob "$destination_blob" "$digest" || return 1
        return 0
    fi

    temp_blob="$(mktemp "$destination_root/blobs/.omove-${filename:7:12}.XXXXXX")" || return 1
    register_temp "$temp_blob"

    if [[ -t 1 ]]; then
        rsync_options+=(--info=progress2)
    fi

    info "Copying blob ${digest:7:12}..."
    rsync "${rsync_options[@]}" -- "$source_blob" "$temp_blob" || {
        error "Blob copy failed: $source_blob"
        return 1
    }

    chown "$OLLAMA_USER:$OLLAMA_GROUP" -- "$temp_blob" || return 1
    chmod 0644 -- "$temp_blob" || return 1
    verify_blob "$temp_blob" "$digest" || return 1
    sync -f "$temp_blob" 2>/dev/null || true

    mv -- "$temp_blob" "$destination_blob" || return 1
    sync -f "$destination_root/blobs" 2>/dev/null || true
    VERIFIED_BLOBS["$destination_blob|$digest"]=1
}

copy_manifest_verified() {
    local source_manifest=$1
    local destination_manifest=$2
    local destination_dir temp_manifest

    destination_dir="$(dirname -- "$destination_manifest")"
    install -d -o "$OLLAMA_USER" -g "$OLLAMA_GROUP" -m 0755 -- "$destination_dir" || return 1

    if [[ -e "$destination_manifest" ]]; then
        [[ -f "$destination_manifest" && ! -L "$destination_manifest" ]] || {
            error "Destination manifest is not a regular file: $destination_manifest"
            return 1
        }
        if ! cmp -s -- "$source_manifest" "$destination_manifest"; then
            error "A different manifest already exists at $destination_manifest"
            return 1
        fi
        return 0
    fi

    temp_manifest="$(mktemp "$destination_dir/.omove-manifest.XXXXXX")" || return 1
    register_temp "$temp_manifest"

    rsync -a --protect-args -- "$source_manifest" "$temp_manifest" || return 1
    cmp -s -- "$source_manifest" "$temp_manifest" || {
        error "Manifest verification failed after copy: $source_manifest"
        return 1
    }

    chown "$OLLAMA_USER:$OLLAMA_GROUP" -- "$temp_manifest" || return 1
    chmod 0644 -- "$temp_manifest" || return 1
    sync -f "$temp_manifest" 2>/dev/null || true
    mv -- "$temp_manifest" "$destination_manifest" || return 1
    sync -f "$destination_dir" 2>/dev/null || true
}

prune_empty_manifest_dirs() {
    local manifest_root=$1
    local removed_manifest=$2
    local directory

    directory="$(dirname -- "$removed_manifest")"
    while [[ "$directory" != "$manifest_root" && "$directory" == "$manifest_root/"* ]]; do
        rmdir -- "$directory" 2>/dev/null || break
        directory="$(dirname -- "$directory")"
    done
}

build_reference_set() {
    local root=$1
    local output_file=$2
    local manifest rel raw digest

    : > "$output_file"

    while IFS= read -r -d '' rel; do
        canonicalize_manifest_relpath "$rel" || {
            error "Invalid manifest path found under $root/manifests: $rel"
            return 1
        }
        manifest="$root/manifests/$rel"

        if ! jq -e '(.config.digest | type == "string") and (.layers | type == "array")' "$manifest" >/dev/null; then
            error "Cannot garbage-collect while a manifest is invalid: $manifest"
            return 1
        fi

        raw="$(jq -r '.config.digest, (.layers[]? | .digest)' "$manifest")" || return 1
        while IFS= read -r digest; do
            [[ "$digest" =~ ^sha256:[0-9a-fA-F]{64}$ ]] || {
                error "Cannot garbage-collect because $manifest contains an invalid digest."
                return 1
            }
            printf '%s\n' "${digest,,}" >> "$output_file"
        done <<< "$raw"
    done < <(find "$root/manifests" -mindepth 2 -maxdepth 4 -type f -printf '%P\0' | sort -z)

    sort -u -o "$output_file" "$output_file"
}

garbage_collect_candidates() {
    local root=$1
    shift
    local -a candidates=("$@")
    local references digest filename blob size
    local reclaimed=0

    references="$(mktemp)" || return 1
    register_temp "$references"

    build_reference_set "$root" "$references" || return 1

    for digest in "${candidates[@]}"; do
        if grep -Fqx -- "$digest" "$references"; then
            continue
        fi

        filename="$(blob_filename "$digest")"
        blob="$root/blobs/$filename"
        [[ -e "$blob" ]] || continue
        [[ -f "$blob" && ! -L "$blob" ]] || {
            error "Refusing to remove non-regular blob path: $blob"
            return 1
        }

        size="$(stat -c '%s' -- "$blob")" || return 1
        rm -f -- "$blob" || return 1
        reclaimed=$((reclaimed + size))
        unset 'VERIFIED_BLOBS[$blob|$digest]' 2>/dev/null || true
    done

    GC_RECLAIMED=$reclaimed
}

transition_model() {
    local operation=$1
    local query=$2
    local source_root destination_root action_past action_direction
    local rel canonical_rel destination_rel legacy_rel source_manifest destination_manifest display
    local source_manifest_hash current_manifest_hash digest
    local logical_size reclaimed_human logical_human
    local -a digests=()

    case "$operation" in
        freeze)
            source_root="$HOT_ROOT"
            destination_root="$COLD_ROOT"
            action_past="Frozen"
            action_direction="cold storage"
            ;;
        thaw)
            source_root="$COLD_ROOT"
            destination_root="$HOT_ROOT"
            action_past="Thawed"
            action_direction="hot storage"
            ;;
        *)
            error "Internal error: unknown transition operation $operation"
            return 1
            ;;
    esac

    resolve_manifest "$source_root" "$query" || return 1
    rel="$RESOLVED_REL"
    canonical_rel="$RESOLVED_CANONICAL_REL"
    destination_rel="$canonical_rel"

    if [[ "$operation" == "freeze" ]]; then
        legacy_rel="$(legacy_relpath_for_canonical "$canonical_rel" 2>/dev/null || true)"
        if [[ -n "$legacy_rel" && -e "$destination_root/manifests/$legacy_rel" ]]; then
            if [[ -e "$destination_root/manifests/$canonical_rel" ]]; then
                error "Both legacy and canonical cold manifests exist for $(manifest_display_name "$canonical_rel")."
                error "Resolve the duplicate archive entries before freezing this model."
                return 1
            fi
            destination_rel="$legacy_rel"
        fi
    fi

    source_manifest="$source_root/manifests/$rel"
    destination_manifest="$destination_root/manifests/$destination_rel"
    display="$(manifest_display_name "$canonical_rel")"

    info "Validating $display in source storage."
    verify_manifest_blobs "$source_root" "$source_manifest" || return 1
    digests=("${MANIFEST_DIGESTS[@]}")
    logical_size=$MANIFEST_LOGICAL_SIZE
    source_manifest_hash="$(sha256sum -- "$source_manifest")" || return 1
    source_manifest_hash="${source_manifest_hash%% *}"

    check_destination_space "$source_root" "$destination_root" "${digests[@]}" || return 1

    for digest in "${digests[@]}"; do
        copy_blob_verified "$source_root" "$destination_root" "$digest" || return 1
    done

    current_manifest_hash="$(sha256sum -- "$source_manifest")" || return 1
    current_manifest_hash="${current_manifest_hash%% *}"
    if [[ "$current_manifest_hash" != "$source_manifest_hash" ]]; then
        error "Source manifest changed during the transaction: $source_manifest"
        error "No source data was removed. Retry the operation."
        return 1
    fi

    copy_manifest_verified "$source_manifest" "$destination_manifest" || return 1
    verify_manifest_blobs "$destination_root" "$destination_manifest" || return 1

    current_manifest_hash="$(sha256sum -- "$source_manifest")" || return 1
    current_manifest_hash="${current_manifest_hash%% *}"
    if [[ "$current_manifest_hash" != "$source_manifest_hash" ]]; then
        error "Source manifest changed before commit: $source_manifest"
        error "Both storage tiers retain a complete copy. Retry the operation."
        return 1
    fi

    rm -f -- "$source_manifest" || {
        error "Failed to remove source manifest after destination commit: $source_manifest"
        return 1
    }
    sync -f "$(dirname -- "$source_manifest")" 2>/dev/null || true
    prune_empty_manifest_dirs "$source_root/manifests" "$source_manifest"

    GC_RECLAIMED=0
    if ! garbage_collect_candidates "$source_root" "${digests[@]}"; then
        warn "$display was transitioned, but source blob cleanup was skipped because reference validation failed."
        GC_RECLAIMED=0
    fi

    logical_human="$(numfmt --to=iec-i --suffix=B "$logical_size")"
    reclaimed_human="$(numfmt --to=iec-i --suffix=B "$GC_RECLAIMED")"
    log "$action_past $display to $action_direction. Logical size: $logical_human; source space reclaimed: $reclaimed_human."
}


move_manifest_to_canonical() {
    local root=$1
    local rel=$2
    local source_manifest destination_manifest destination_dir display

    MIGRATED_REL="$rel"
    MIGRATED_CHANGED=0
    canonicalize_manifest_relpath "$rel" || {
        error "Cannot migrate invalid manifest path: $rel"
        return 1
    }

    if [[ "$rel" == "$CANONICAL_REL" ]]; then
        return 0
    fi

    source_manifest="$root/manifests/$rel"
    destination_manifest="$root/manifests/$CANONICAL_REL"
    destination_dir="$(dirname -- "$destination_manifest")"
    display="$(manifest_display_name "$CANONICAL_REL")"

    install -d -o "$OLLAMA_USER" -g "$OLLAMA_GROUP" -m 0755 -- "$destination_dir" || return 1

    if [[ -e "$destination_manifest" ]]; then
        [[ -f "$destination_manifest" && ! -L "$destination_manifest" ]] || {
            error "Canonical manifest destination is not a regular file: $destination_manifest"
            return 1
        }
        if ! cmp -s -- "$source_manifest" "$destination_manifest"; then
            error "Conflicting canonical manifest already exists for $display"
            error "Legacy:    $source_manifest"
            error "Canonical: $destination_manifest"
            return 1
        fi
        rm -f -- "$source_manifest" || return 1
    else
        mv -- "$source_manifest" "$destination_manifest" || return 1
    fi

    chown "$OLLAMA_USER:$OLLAMA_GROUP" -- "$destination_manifest" || return 1
    chmod 0644 -- "$destination_manifest" || return 1
    sync -f "$destination_dir" 2>/dev/null || true
    prune_empty_manifest_dirs "$root/manifests" "$source_manifest"

    MIGRATED_REL="$CANONICAL_REL"
    MIGRATED_CHANGED=1
    MIGRATION_MANIFESTS=$((MIGRATION_MANIFESTS + 1))
    log "MIGRATED $display"
}

repair_cold_manifest_from_hot() {
    local rel=$1
    local manifest="$COLD_ROOT/manifests/$rel"
    local display digest filename cold_blob hot_blob
    local -a copyable=()
    local -a unresolved=()

    canonicalize_manifest_relpath "$rel" || return 1
    display="$(manifest_display_name "$CANONICAL_REL")"
    load_manifest "$manifest" || return 1

    for digest in "${MANIFEST_DIGESTS[@]}"; do
        filename="$(blob_filename "$digest")"
        cold_blob="$COLD_ROOT/blobs/$filename"
        hot_blob="$HOT_ROOT/blobs/$filename"

        if [[ -e "$cold_blob" ]]; then
            [[ -f "$cold_blob" && ! -L "$cold_blob" ]] || {
                error "Cold blob path is not a regular file: $cold_blob"
                return 1
            }
            continue
        fi

        if [[ -f "$hot_blob" && ! -L "$hot_blob" ]]; then
            copyable+=("$digest")
        else
            unresolved+=("$digest")
        fi
    done

    if (( ${#copyable[@]} > 0 )); then
        check_destination_space "$HOT_ROOT" "$COLD_ROOT" "${copyable[@]}" || return 1
        for digest in "${copyable[@]}"; do
            copy_blob_verified "$HOT_ROOT" "$COLD_ROOT" "$digest" || return 1
            MIGRATION_BLOBS=$((MIGRATION_BLOBS + 1))
        done
        log "REPAIRED $display (${#copyable[@]} blob(s) copied from hot storage)"
    fi

    if (( ${#unresolved[@]} > 0 )); then
        error "$display still has ${#unresolved[@]} blob(s) unavailable in both cold and hot storage:"
        for digest in "${unresolved[@]}"; do
            printf '  %s\n' "$digest" >&2
        done
        MIGRATION_UNRESOLVED=$((MIGRATION_UNRESOLVED + 1))
        return 1
    fi

    return 0
}

migrate_cold_rel() {
    local rel=$1
    local current_rel
    local layout_rc=0
    local repair_rc=0

    canonicalize_manifest_relpath "$rel" || {
        error "Invalid cold manifest path: $rel"
        return 1
    }

    # Cold manifests may be canonicalized even when blobs are still missing.
    # This changes metadata layout only and never deletes a blob.
    move_manifest_to_canonical "$COLD_ROOT" "$rel" || layout_rc=1
    (( layout_rc == 0 )) || return 1
    current_rel="$MIGRATED_REL"

    repair_cold_manifest_from_hot "$current_rel" || repair_rc=1
    return "$repair_rc"
}

migrate_hot_rel() {
    local rel=$1

    canonicalize_manifest_relpath "$rel" || {
        error "Invalid hot manifest path: $rel"
        return 1
    }

    [[ "$rel" != "$CANONICAL_REL" ]] || return 0

    # Do not make a previously ignored malformed hot manifest visible to Ollama
    # unless every referenced blob is present and valid.
    verify_manifest_blobs "$HOT_ROOT" "$HOT_ROOT/manifests/$rel" || return 1
    move_manifest_to_canonical "$HOT_ROOT" "$rel"
}

migrate_store() {
    local tier=$1
    shift
    local root rel query
    local status=0
    local -a requested=("$@")

    case "$tier" in
        cold) root="$COLD_ROOT" ;;
        hot)  root="$HOT_ROOT" ;;
        *)
            error "migrate tier must be 'cold' or 'hot'."
            return 2
            ;;
    esac

    if (( ${#requested[@]} > 0 )); then
        for query in "${requested[@]}"; do
            if ! resolve_manifest "$root" "$query"; then
                status=1
                continue
            fi
            rel="$RESOLVED_REL"
            if [[ "$tier" == "cold" ]]; then
                migrate_cold_rel "$rel" || status=1
            else
                migrate_hot_rel "$rel" || status=1
            fi
        done
        return "$status"
    fi

    # Snapshot paths before migration because successful migrations rename files.
    local -a rels=()
    while IFS= read -r -d '' rel; do
        rels+=("$rel")
    done < <(find "$root/manifests" -mindepth 2 -maxdepth 4 -type f -printf '%P\0' | sort -z)

    for rel in "${rels[@]}"; do
        [[ -e "$root/manifests/$rel" ]] || continue
        if [[ "$tier" == "cold" ]]; then
            migrate_cold_rel "$rel" || status=1
        else
            migrate_hot_rel "$rel" || status=1
        fi
    done

    return "$status"
}

migration_summary() {
    log "Migration summary: $MIGRATION_MANIFESTS manifest(s) canonicalized; $MIGRATION_BLOBS blob(s) restored from hot storage; $MIGRATION_UNRESOLVED model(s) remain incomplete."
}

list_store() {
    local tier=$1
    local root rel manifest display manifest_hash logical_size logical_human modified status
    local digest filename missing legacy_layout

    case "$tier" in
        cold) root="$COLD_ROOT" ;;
        hot)  root="$HOT_ROOT" ;;
        *)
            error "list tier must be 'cold' or 'hot'."
            return 2
            ;;
    esac

    printf '%-52s %-12s %-10s %-12s %-10s\n' "NAME" "ID" "SIZE" "MODIFIED" "STATUS"
    printf '%s\n' "------------------------------------------------------------------------------------------------------"

    while IFS= read -r -d '' rel; do
        manifest="$root/manifests/$rel"

        if ! canonicalize_manifest_relpath "$rel"; then
            printf '%-52s %-12s %-10s %-12s %-10s\n' "$rel" "-" "-" "-" "BAD-PATH"
            continue
        fi

        legacy_layout=0
        [[ "$rel" == "$CANONICAL_REL" ]] || legacy_layout=1
        display="$(manifest_display_name "$rel")"
        manifest_hash="$(sha256sum -- "$manifest" 2>/dev/null || true)"
        manifest_hash="${manifest_hash%% *}"
        manifest_hash="${manifest_hash:0:12}"
        modified="$(date -d "@$(stat -c '%Y' -- "$manifest")" '+%Y-%m-%d' 2>/dev/null || printf '-')"

        if ! load_manifest "$manifest"; then
            printf '%-52s %-12s %-10s %-12s %-10s\n' "$display" "${manifest_hash:--}" "-" "$modified" "INVALID"
            continue
        fi

        logical_size=$MANIFEST_LOGICAL_SIZE
        logical_human="$(numfmt --to=iec-i --suffix=B "$logical_size")"
        missing=0
        for digest in "${MANIFEST_DIGESTS[@]}"; do
            filename="$(blob_filename "$digest")"
            [[ -f "$root/blobs/$filename" && ! -L "$root/blobs/$filename" ]] || missing=$((missing + 1))
        done

        if (( missing == 0 )); then
            if (( legacy_layout == 1 )); then
                status="LEGACY"
            else
                status="OK"
            fi
        else
            status="MISSING:$missing"
        fi

        printf '%-52s %-12s %-10s %-12s %-10s\n' \
            "$display" "$manifest_hash" "$logical_human" "$modified" "$status"
    done < <(find "$root/manifests" -mindepth 2 -maxdepth 4 -type f -printf '%P\0' | sort -z)
}

verify_one() {
    local root=$1
    local rel=$2
    local manifest="$root/manifests/$rel"
    local display

    canonicalize_manifest_relpath "$rel" || return 1
    display="$(manifest_display_name "$CANONICAL_REL")"
    if verify_manifest_blobs "$root" "$manifest"; then
        log "OK     $display"
        return 0
    fi

    log "BROKEN $display"
    return 1
}

verify_store() {
    local tier=$1
    shift
    local root rel query
    local status=0
    local -a requested=("$@")

    case "$tier" in
        cold) root="$COLD_ROOT" ;;
        hot)  root="$HOT_ROOT" ;;
        *)
            error "verify tier must be 'cold' or 'hot'."
            return 2
            ;;
    esac

    if (( ${#requested[@]} > 0 )); then
        for query in "${requested[@]}"; do
            if resolve_manifest "$root" "$query"; then
                rel="$RESOLVED_REL"
                verify_one "$root" "$rel" || status=1
            else
                status=1
            fi
        done
        return "$status"
    fi

    while IFS= read -r -d '' rel; do
        if ! canonicalize_manifest_relpath "$rel"; then
            error "Invalid manifest path: $rel"
            status=1
            continue
        fi
        verify_one "$root" "$rel" || status=1
    done < <(find "$root/manifests" -mindepth 2 -maxdepth 4 -type f -printf '%P\0' | sort -z)

    return "$status"
}

main() {
    local command_name="${1:-}"
    local tier status model

    case "$command_name" in
        help|-h|--help)
            usage
            return 0
            ;;
        version|-V|--version)
            printf 'omove %s\n' "$VERSION"
            return 0
            ;;
        list)
            shift
            (( $# <= 1 )) || { usage >&2; return 2; }
            tier="${1:-cold}"
            prepare_read_operation "$@"
            list_store "$tier"
            ;;
        verify)
            shift
            tier="cold"
            if (( $# > 0 )) && [[ "$1" == "cold" || "$1" == "hot" ]]; then
                tier=$1
                shift
            fi
            prepare_read_operation "$@"
            verify_store "$tier" "$@"
            ;;
        migrate)
            shift
            tier="all"
            if (( $# > 0 )) && [[ "$1" == "all" || "$1" == "cold" || "$1" == "hot" ]]; then
                tier=$1
                shift
            fi
            if [[ "$tier" == "all" && $# -gt 0 ]]; then
                error "Model selection requires an explicit migrate tier: cold or hot."
                usage >&2
                return 2
            fi
            prepare_mutation "$@"
            status=0
            if [[ "$tier" == "all" || "$tier" == "hot" ]]; then
                migrate_store hot "$@" || status=1
            fi
            if [[ "$tier" == "all" || "$tier" == "cold" ]]; then
                migrate_store cold "$@" || status=1
            fi
            migration_summary
            return "$status"
            ;;
        freeze)
            shift
            (( $# > 0 )) || { error "freeze requires at least one model name."; usage >&2; return 2; }
            prepare_mutation "$@"
            status=0
            for model in "$@"; do
                if ! transition_model freeze "$model"; then
                    status=1
                fi
            done
            return "$status"
            ;;
        thaw)
            shift
            (( $# > 0 )) || { error "thaw requires at least one model name."; usage >&2; return 2; }
            prepare_mutation "$@"
            status=0
            for model in "$@"; do
                if ! transition_model thaw "$model"; then
                    status=1
                fi
            done
            return "$status"
            ;;
        "")
            usage >&2
            return 2
            ;;
        *)
            error "Unknown command: $command_name"
            usage >&2
            return 2
            ;;
    esac
}

main "$@"

