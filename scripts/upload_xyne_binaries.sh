#!/usr/bin/env bash
# =============================================================================
# upload_xyne_binaries.sh — ONE-TIME: publish the prebuilt xyne-cli linux
# binaries to GCS so every Batch VM can fetch them (the npm package isn't
# linux-ready and the local build chain is fragile, so we ship binaries).
#
# WHAT IT UPLOADS  (paths/URIs from ../config.yaml  xyne_binary:)
#   binaries/xyne-linux-x64
#   binaries/xyne-linux-arm64
#   binaries/package.json
#
# INTEGRITY
#   * Each file gets a side-car .sha256 uploaded next to it;
#     fetch_xyne_binaries.sh re-verifies on download.
#   * Each object is staged to a .tmp name then promoted with `gsutil mv`, so
#     a partially-uploaded binary is never visible at the final URI.
#   * Refuses to upload a file that is missing or zero-byte.
#   * Fails loudly (non-zero) on any error.
#
# USAGE (run once, from your build machine that has the freshly built
#        binaries and GCS write access; permissions are yours to handle):
#     ./scripts/upload_xyne_binaries.sh
#     ./scripts/upload_xyne_binaries.sh --dry-run
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

DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown flag: $1 (try --help)" ;;
  esac
done

[ -f "$CONFIG" ] || die "config.yaml not found at $CONFIG"
command -v gsutil >/dev/null 2>&1 || command -v gcloud >/dev/null 2>&1 \
  || die "Neither gsutil nor gcloud found — cannot upload to GCS."

# Emit "<local_path>\t<gcs_uri>" lines from config.yaml xyne_binary.files[].
mapfile -t ENTRIES < <(python3 - "$CONFIG" <<'PY'
import sys
try:
    import yaml
except Exception:
    sys.exit("PyYAML required: pip install pyyaml")
cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
xb = (cfg.get("xyne_binary") or {})
local_dir = xb.get("local_dir", "binaries")
for f in (xb.get("files") or []):
    print(f"{local_dir}/{f['name']}\t{f['gcs_uri']}")
PY
)
[ "${#ENTRIES[@]}" -gt 0 ] || die "No xyne_binary.files entries in config.yaml"

gcs_cp() {
  if command -v gsutil >/dev/null 2>&1; then gsutil -q cp "$1" "$2"
  else gcloud storage cp "$1" "$2"; fi
}
gcs_mv() {
  if command -v gsutil >/dev/null 2>&1; then gsutil -q mv "$1" "$2"
  else gcloud storage mv "$1" "$2"; fi
}
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}

info "Config     : $CONFIG"
[ "$DRY_RUN" -eq 1 ] && warn "DRY-RUN: validating only, nothing uploaded"

# Preflight: every declared file must exist and be non-empty.
for entry in "${ENTRIES[@]}"; do
  rel="${entry%%$'\t'*}"
  abs="${REPO_DIR}/${rel}"
  [ -e "$abs" ] || die "missing local file: $abs (build the binaries first)"
  [ -s "$abs" ] || die "zero-byte local file: $abs"
  sz=$(du -h "$abs" | cut -f1)
  info "  $rel ($sz)  ->  ${entry##*$'\t'}"
done

if [ "$DRY_RUN" -eq 1 ]; then
  ok "DRY-RUN complete — all declared binaries present, preflight passed."
  exit 0
fi

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

for entry in "${ENTRIES[@]}"; do
  rel="${entry%%$'\t'*}"
  uri="${entry##*$'\t'}"
  abs="${REPO_DIR}/${rel}"
  base="$(basename "$rel")"

  info "Hashing $base …"
  h="$(sha256_of "$abs")"
  echo "$h  $base" > "${WORK}/${base}.sha256"
  ok "  sha256: $h"

  info "Uploading $base (staged) …"
  gcs_cp "$abs"                    "${uri}.tmp"        || die "upload failed: $base"
  gcs_cp "${WORK}/${base}.sha256"  "${uri}.sha256.tmp" || die "upload failed: ${base}.sha256"

  info "Promoting $base …"
  gcs_mv "${uri}.tmp"        "$uri"          || die "promote failed: $base"
  gcs_mv "${uri}.sha256.tmp" "${uri}.sha256" || die "promote failed: ${base}.sha256"
  ok "  $uri"
done

ok "All xyne binaries uploaded. Batch VMs restore them via"
ok "setup.sh → fetch_xyne_binaries.sh"
