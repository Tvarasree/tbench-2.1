#!/usr/bin/env bash
# =============================================================================
# fetch_xyne_binaries.sh — setup.sh helper. Restores the prebuilt xyne-cli
# linux binaries into ./binaries from the GCS objects published once by
# upload_xyne_binaries.sh.
#
# FLOW (per file in config.yaml xyne_binary.files[]):
#   1. If a local copy already exists and matches the published .sha256
#      -> skip (idempotent, survives re-runs / resumed setup).
#   2. Else download the object + its .sha256, VERIFY, then atomically move
#      it into place (download to .part, rename on success).
#
# This is best-effort: a missing/ξunreachable binary is a WARNING, not a hard
# failure, because only the arch actually used by the task containers is
# required (the adapter raises a clear error if its arch's binary is absent).
# Exit non-zero only if NOTHING could be fetched at all.
#
# Reads config.yaml: xyne_binary.local_dir, xyne_binary.files[].{name,gcs_uri}
# =============================================================================
set -uo pipefail   # NOT -e: per-file best-effort.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="${REPO_DIR}/config.yaml"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[info]  $*${NC}"; }
ok()    { echo -e "${GREEN}[ok]    $*${NC}"; }
warn()  { echo -e "${YELLOW}[warn]  $*${NC}"; }
err()   { echo -e "${RED}[error] $*${NC}" >&2; }

[ -f "$CONFIG" ] || { err "config.yaml not found at $CONFIG"; exit 2; }

LOCAL_DIR_REL="$(python3 - "$CONFIG" <<'PY' 2>/dev/null || echo binaries
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
print((cfg.get("xyne_binary") or {}).get("local_dir", "binaries"))
PY
)"
LOCAL_DIR="${REPO_DIR}/${LOCAL_DIR_REL}"

mapfile -t ENTRIES < <(python3 - "$CONFIG" <<'PY' 2>/dev/null
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
xb = cfg.get("xyne_binary") or {}
for f in (xb.get("files") or []):
    print(f"{f['name']}\t{f['gcs_uri']}")
PY
)

if [ "${#ENTRIES[@]}" -eq 0 ]; then
  warn "No xyne_binary.files in config.yaml — skipping binary fetch"
  exit 0
fi

# binaries/ may be a symlink to a sibling checkout (local dev). On a fresh VM
# it won't exist — create a real dir. Never clobber an existing symlink/dir.
if [ ! -e "$LOCAL_DIR" ]; then
  mkdir -p "$LOCAL_DIR"
fi

gcs_cp() {
  if command -v gsutil >/dev/null 2>&1; then gsutil -q cp "$1" "$2"
  elif command -v gcloud >/dev/null 2>&1; then gcloud storage cp "$1" "$2"
  else return 127; fi
}
sha256_hex() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}

any_ok=0
total=0
for entry in "${ENTRIES[@]}"; do
  total=$((total+1))
  name="${entry%%$'\t'*}"
  uri="${entry##*$'\t'}"
  dest="${LOCAL_DIR}/${name}"
  tmp="${dest}.part"

  # Pull the published hash first (cheap) so we can short-circuit if the local
  # copy is already correct.
  expected=""
  if gcs_cp "${uri}.sha256" "${tmp}.sha256" 2>/dev/null; then
    expected="$(awk '{print $1}' "${tmp}.sha256" 2>/dev/null)"
    rm -f "${tmp}.sha256"
  fi

  if [ -s "$dest" ] && [ -n "$expected" ] && [ "$(sha256_hex "$dest")" = "$expected" ]; then
    ok "$name already present and verified — skipping"
    any_ok=1
    continue
  fi

  info "Fetching $name …"
  if ! gcs_cp "$uri" "$tmp"; then
    warn "Could not download $name from $uri (skipping; only the task arch is required)"
    rm -f "$tmp"
    continue
  fi

  if [ -n "$expected" ]; then
    got="$(sha256_hex "$tmp")"
    if [ "$got" != "$expected" ]; then
      err "sha256 MISMATCH for $name (got $got, want $expected) — discarding"
      rm -f "$tmp"
      continue
    fi
    ok "  sha256 verified"
  else
    warn "  no .sha256 side-car for $name — skipping verification"
  fi

  chmod +x "$tmp" 2>/dev/null || true   # binaries need +x; harmless on json
  mv -f "$tmp" "$dest"
  ok "  -> $dest"
  any_ok=1
done

if [ "$any_ok" -eq 1 ]; then
  ok "xyne binary fetch complete ($(ls -1 "$LOCAL_DIR" 2>/dev/null | wc -l | tr -d ' ') file(s) in $LOCAL_DIR)"
  exit 0
fi

err "Could not fetch ANY xyne binary from GCS. The xyne-cli agent will fail"
err "until binaries are present. (claude-code / opencode / etc. still work.)"
exit 1
