#!/usr/bin/env bash
# =============================================================================
# fetch_dataset_tarball.sh — setup.sh helper. Restores the harbor
# terminal-bench task packages onto a fresh VM from the GCS tarball produced
# by make_dataset_tarball.sh, with a `harbor download` fallback.
#
# FLOW
#   1. If the cache already looks populated  -> skip (idempotent).
#   2. Else download <gcs_uri> + <gcs_uri>.sha256, VERIFY the hash, then
#      extract into ~/.cache/harbor (archive paths are <subdir>/...).
#   3. If GCS is unavailable / hash mismatch / extraction fails -> fall back
#      to `harbor download <dataset> --cache` so a VM is never left without
#      tasks.
#
# Exit non-zero only if BOTH the tarball path and the fallback fail.
#
# Reads config.yaml: dataset.name, dataset.harbor_cache_subdir,
# dataset.tarball.gcs_uri, dataset.tarball.sha256_gcs_uri.
# =============================================================================
set -uo pipefail   # NOT -e: we intentionally try fallbacks on failure.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="${REPO_DIR}/config.yaml"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[info]  $*${NC}"; }
ok()    { echo -e "${GREEN}[ok]    $*${NC}"; }
warn()  { echo -e "${YELLOW}[warn]  $*${NC}"; }
err()   { echo -e "${RED}[error] $*${NC}" >&2; }

yaml_get() {
  python3 - "$CONFIG" "$1" <<'PY' 2>/dev/null
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

[ -f "$CONFIG" ] || { err "config.yaml not found at $CONFIG"; exit 2; }

DATASET_NAME="$(yaml_get dataset.name || echo 'terminal-bench/terminal-bench-2-1')"
CACHE_SUBDIR="$(yaml_get dataset.harbor_cache_subdir || echo tasks)"
GCS_URI="$(yaml_get dataset.tarball.gcs_uri || true)"
SHA_URI="$(yaml_get dataset.tarball.sha256_gcs_uri || true)"
[ -n "${SHA_URI:-}" ] || SHA_URI="${GCS_URI}.sha256"

HARBOR_ROOT="${HOME}/.cache/harbor"
CACHE_DIR="${HARBOR_ROOT}/${CACHE_SUBDIR}"

# ---------------------------------------------------------------------------
# 1. Idempotency — already populated?
# ---------------------------------------------------------------------------
existing=$(find "$CACHE_DIR" -name task.toml 2>/dev/null | head -5 | wc -l | tr -d ' ')
if [ "${existing:-0}" -ge 1 ]; then
  n=$(find "$CACHE_DIR" -name task.toml 2>/dev/null | wc -l | tr -d ' ')
  ok "Harbor task cache already populated ($n task.toml under $CACHE_DIR) — skipping fetch"
  exit 0
fi

gcs_cp() {
  if command -v gsutil >/dev/null 2>&1; then gsutil -q cp "$1" "$2"
  elif command -v gcloud >/dev/null 2>&1; then gcloud storage cp "$1" "$2"
  else return 127; fi
}
sha256_check() {  # sha256_check <file> <expected_hex>
  local got
  if command -v sha256sum >/dev/null 2>&1; then got=$(sha256sum "$1" | awk '{print $1}')
  else got=$(shasum -a 256 "$1" | awk '{print $1}'); fi
  [ "$got" = "$2" ]
}

harbor_download_fallback() {
  warn "Falling back to: harbor download ${DATASET_NAME} --cache"
  if ! command -v harbor >/dev/null 2>&1; then
    export PATH="$HOME/.local/bin:$PATH"
  fi
  if command -v harbor >/dev/null 2>&1; then
    if harbor download "$DATASET_NAME" --cache; then
      ok "Dataset restored via harbor download"
      return 0
    fi
  else
    err "harbor not on PATH — cannot run download fallback"
  fi
  return 1
}

# ---------------------------------------------------------------------------
# 2. Tarball path
# ---------------------------------------------------------------------------
if [ -z "${GCS_URI:-}" ]; then
  warn "dataset.tarball.gcs_uri not set in config — using harbor download"
  harbor_download_fallback; exit $?
fi

case "$GCS_URI" in
  *.tar.zst) DECOMP=(--zstd) ;;
  *.tar.gz|*.tgz) DECOMP=(--gzip) ;;
  *) warn "Unknown tarball extension ($GCS_URI) — using harbor download"
     harbor_download_fallback; exit $? ;;
esac

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
LOCAL_TAR="${WORK}/$(basename "$GCS_URI")"

info "Downloading dataset tarball: $GCS_URI"
if ! gcs_cp "$GCS_URI" "$LOCAL_TAR"; then
  warn "Could not download tarball from GCS (no gsutil/gcloud or object missing)"
  harbor_download_fallback; exit $?
fi

# Verify integrity if a sha256 side-car is available. Missing side-car is a
# warning (older uploads) but a present-and-mismatching one is fatal → fallback.
if gcs_cp "$SHA_URI" "${LOCAL_TAR}.sha256" 2>/dev/null; then
  EXPECTED=$(awk '{print $1}' "${LOCAL_TAR}.sha256")
  if [ -n "$EXPECTED" ] && sha256_check "$LOCAL_TAR" "$EXPECTED"; then
    ok "sha256 verified ($EXPECTED)"
  else
    err "sha256 MISMATCH for $LOCAL_TAR — refusing to extract a corrupt archive"
    harbor_download_fallback; exit $?
  fi
else
  warn "No sha256 side-car at $SHA_URI — proceeding without hash verification"
fi

# ---------------------------------------------------------------------------
# 3. Extract into ~/.cache/harbor (archive entries are <subdir>/...)
# ---------------------------------------------------------------------------
mkdir -p "$HARBOR_ROOT"
info "Extracting into $HARBOR_ROOT …"
if ! tar "${DECOMP[@]}" -xf "$LOCAL_TAR" -C "$HARBOR_ROOT"; then
  err "Extraction failed"
  harbor_download_fallback; exit $?
fi

n=$(find "$CACHE_DIR" -name task.toml 2>/dev/null | wc -l | tr -d ' ')
if [ "${n:-0}" -lt 1 ]; then
  err "Extraction produced no task.toml under $CACHE_DIR"
  harbor_download_fallback; exit $?
fi

ok "Dataset restored from GCS tarball ($n task.toml under $CACHE_DIR)"
exit 0
