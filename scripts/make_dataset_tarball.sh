#!/usr/bin/env bash
# =============================================================================
# make_dataset_tarball.sh — ONE-TIME: snapshot the harbor terminal-bench task
# packages and upload them to GCS so every Batch VM can restore them offline.
#
# WHAT THIS PACKAGES
#   ~/.cache/harbor/<subdir>  (default subdir: tasks)
#   i.e. the small task.toml / Dockerfile / tests / instruction.md files —
#   NOT the heavy Docker images (those go to GAR via seed_gar_images.py).
#
# INTEGRITY
#   * Deterministic tar (sorted entries, pinned mtime/owner) so re-runs that
#     see identical inputs produce an identical archive.
#   * A side-car .sha256 is computed locally and uploaded alongside the
#     tarball; fetch_dataset_tarball.sh re-verifies it on download.
#   * Upload is staged to a .tmp object then promoted with `gsutil mv`, so a
#     half-uploaded archive is never visible at the final URI.
#   * The script fails loudly (non-zero) if any step fails.
#
# USAGE (run once, from a machine that has the harbor cache + gsutil + GCS
#        write access; permissions are yours to handle):
#     ./scripts/make_dataset_tarball.sh
#     ./scripts/make_dataset_tarball.sh --dry-run
#     ./scripts/make_dataset_tarball.sh --gcs-uri gs://bucket/path/tasks.tar.zst
#
# The default GCS URI + cache subdir are read from ../config.yaml.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="${REPO_DIR}/config.yaml"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[info]  $*${NC}"; }
ok()    { echo -e "${GREEN}[ok]    $*${NC}"; }
warn()  { echo -e "${YELLOW}[warn]  $*${NC}"; }
die()   { echo -e "${RED}[error] $*${NC}" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Tiny YAML leaf reader (no PyYAML dependency for a shell prereq script).
# Only used for simple "key: value" scalars under known parents.
# ---------------------------------------------------------------------------
yaml_get() {
  # $1 = python-style dotted path e.g. dataset.tarball.gcs_uri
  python3 - "$CONFIG" "$1" <<'PY'
import sys
try:
    import yaml
except Exception:
    sys.exit(3)
cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
cur = cfg
for part in sys.argv[2].split("."):
    if not isinstance(cur, dict) or part not in cur:
        sys.exit(4)
    cur = cur[part]
print(cur if cur is not None else "")
PY
}

DRY_RUN=0
GCS_URI=""
SHA_URI=""
CACHE_SUBDIR=""

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)      DRY_RUN=1; shift ;;
    --gcs-uri)      GCS_URI="$2"; shift 2 ;;
    --cache-subdir) CACHE_SUBDIR="$2"; shift 2 ;;
    -h|--help)
      grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown flag: $1 (try --help)" ;;
  esac
done

[ -f "$CONFIG" ] || die "config.yaml not found at $CONFIG"

if [ -z "$GCS_URI" ]; then
  GCS_URI="$(yaml_get dataset.tarball.gcs_uri || true)"
fi
if [ -z "$SHA_URI" ]; then
  SHA_URI="$(yaml_get dataset.tarball.sha256_gcs_uri || true)"
fi
if [ -z "$CACHE_SUBDIR" ]; then
  CACHE_SUBDIR="$(yaml_get dataset.harbor_cache_subdir || echo tasks)"
fi
[ -n "$GCS_URI" ] || die "dataset.tarball.gcs_uri missing in config and no --gcs-uri"
[ -n "$SHA_URI" ] || SHA_URI="${GCS_URI}.sha256"

CACHE_DIR="${HOME}/.cache/harbor/${CACHE_SUBDIR}"

# Choose compressor from the configured object extension so config.yaml stays
# the single source of truth and fetch can mirror it deterministically.
case "$GCS_URI" in
  *.tar.zst)  COMPRESS="zstd"; TAR_COMP=(--zstd) ;;
  *.tar.gz)   COMPRESS="gzip"; TAR_COMP=(--gzip) ;;
  *.tgz)      COMPRESS="gzip"; TAR_COMP=(--gzip) ;;
  *) die "Unsupported tarball extension in $GCS_URI (want .tar.zst / .tar.gz / .tgz)" ;;
esac

info "Source cache : $CACHE_DIR"
info "GCS object   : $GCS_URI"
info "GCS sha256   : $SHA_URI"
info "Compressor   : $COMPRESS"
[ "$DRY_RUN" -eq 1 ] && warn "DRY-RUN: will not tar or upload"

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
[ -d "$CACHE_DIR" ] || die "Harbor cache dir not found: $CACHE_DIR
  Populate it first:  harbor download $(yaml_get dataset.name || echo terminal-bench/terminal-bench-2-1) --cache"

N_TOML=$(find "$CACHE_DIR" -name task.toml 2>/dev/null | wc -l | tr -d ' ')
[ "$N_TOML" -gt 0 ] || die "No task.toml under $CACHE_DIR — cache looks empty/corrupt"
info "Found $N_TOML task.toml file(s) in the cache."

if [ "$COMPRESS" = "zstd" ] && ! command -v zstd >/dev/null 2>&1; then
  die "zstd not installed but config requests a .tar.zst object.
  Install zstd, or set dataset.tarball.gcs_uri to a .tar.gz path."
fi
command -v gsutil >/dev/null 2>&1 || command -v gcloud >/dev/null 2>&1 \
  || die "Neither gsutil nor gcloud found — cannot upload to GCS."

gcs_cp() {  # gcs_cp <src> <dst>
  if command -v gsutil >/dev/null 2>&1; then gsutil -q cp "$1" "$2"
  else gcloud storage cp "$1" "$2"; fi
}
gcs_mv() {
  if command -v gsutil >/dev/null 2>&1; then gsutil -q mv "$1" "$2"
  else gcloud storage mv "$1" "$2"; fi
}

if [ "$DRY_RUN" -eq 1 ]; then
  ok "DRY-RUN complete — preflight passed, nothing uploaded."
  exit 0
fi

# ---------------------------------------------------------------------------
# Build a deterministic archive in a temp dir
# ---------------------------------------------------------------------------
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
LOCAL_TAR="${WORK}/$(basename "$GCS_URI")"

info "Creating deterministic archive…"
# Deterministic: sorted names, fixed mtime/uid/gid/numeric-owner. tar is run
# with -C so paths inside the archive are relative to ~/.cache/harbor (the
# extraction root used by fetch_dataset_tarball.sh).
TAR_FLAGS=(--sort=name --mtime='UTC 2020-01-01' --owner=0 --group=0 --numeric-owner)
if tar --version 2>/dev/null | grep -qi 'gnu tar'; then
  tar "${TAR_FLAGS[@]}" "${TAR_COMP[@]}" \
      -C "${HOME}/.cache/harbor" -cf "$LOCAL_TAR" "$CACHE_SUBDIR"
else
  warn "GNU tar not detected (BSD/macOS tar) — archive still valid, just not"
  warn "bit-for-bit reproducible across machines. Content integrity is still"
  warn "guaranteed by the uploaded .sha256."
  tar "${TAR_COMP[@]}" -C "${HOME}/.cache/harbor" -cf "$LOCAL_TAR" "$CACHE_SUBDIR"
fi

SIZE=$(du -h "$LOCAL_TAR" | cut -f1)
ok "Archive built: $LOCAL_TAR ($SIZE)"

# Integrity hash (portable: shasum on macOS, sha256sum on Linux).
if command -v sha256sum >/dev/null 2>&1; then
  ( cd "$WORK" && sha256sum "$(basename "$LOCAL_TAR")" > "${LOCAL_TAR}.sha256" )
else
  ( cd "$WORK" && shasum -a 256 "$(basename "$LOCAL_TAR")" > "${LOCAL_TAR}.sha256" )
fi
HASH=$(awk '{print $1}' "${LOCAL_TAR}.sha256")
ok "sha256: $HASH"

# ---------------------------------------------------------------------------
# Upload: stage to .tmp then promote so the final URI is never half-written.
# ---------------------------------------------------------------------------
info "Uploading archive (staged)…"
gcs_cp "$LOCAL_TAR" "${GCS_URI}.tmp" || die "upload of archive failed"
gcs_cp "${LOCAL_TAR}.sha256" "${SHA_URI}.tmp" || die "upload of sha256 failed"

info "Promoting staged objects…"
gcs_mv "${GCS_URI}.tmp" "$GCS_URI" || die "promote of archive failed"
gcs_mv "${SHA_URI}.tmp" "$SHA_URI" || die "promote of sha256 failed"

ok "Uploaded:"
ok "  $GCS_URI"
ok "  $SHA_URI"
info "Batch VMs will restore this via setup.sh → fetch_dataset_tarball.sh"
