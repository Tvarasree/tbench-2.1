#!/usr/bin/env bash
# =============================================================================
# terminal-bench setup — provisions a Batch VM to run the harbor-based
# terminal-bench harness (xyne-cli + mainstream harbor agents) on grid.ai
# models, pulling task images from Google Artifact Registry.
#
# Target environment (mirrors swe-auto-eval): an x86_64 Debian/Ubuntu runner
# container on a Container-Optimized OS (COS) Batch VM, with the host Docker
# socket bind-mounted in (DooD — no Docker-in-Docker). Runs as root; COS has
# no sudo and a read-only root fs, so gcloud is installed under /var/lib/docker
# (a writable data partition), exactly like swe-auto-eval.
#
# setup.sh is pure provisioning: it IGNORES the [API_KEY] EVAL_RUN_ID [--flags]
# argv the eval-runner passes (run.sh consumes those). Args are accepted and
# ignored so the harness contract is satisfied.
# =============================================================================

# Re-exec under stdbuf so every descendant inherits line-buffered stdio —
# otherwise glibc block-buffers when stdout is a file and the host log_sync
# uploads stale logs. Guard prevents a re-exec loop.
#
# Note: exec `bash "$0"`, NOT `"$0"`. stdbuf resolves a slash-less argv[0]
# (e.g. when invoked as `bash setup.sh`) via $PATH, not the cwd, so
# `exec stdbuf "$0"` would fail with "No such file or directory". `bash` is
# always on PATH and resolves "$0" relative to the cwd, matching how the
# script was just invoked. Works for `bash setup.sh`, `./setup.sh`, and the
# eval-runner's absolute-path invocation alike.
if [ -z "${STDBUF_APPLIED:-}" ] && command -v stdbuf &>/dev/null; then
  export STDBUF_APPLIED=1
  exec stdbuf -oL -eL bash "$0" "$@"
fi

set -u
export DEBIAN_FRONTEND=noninteractive
export TZ=Etc/UTC
export PYTHONUNBUFFERED=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()   { echo -e "${BLUE}[info]  $*${NC}"; }
ok()     { echo -e "${GREEN}[ok]    $*${NC}"; }
warn()   { echo -e "${YELLOW}[warn]  $*${NC}"; }
die()    { echo -e "${RED}[error] $*${NC}" >&2; exit 1; }
header() { echo -e "\n${BLUE}══ $* ══${NC}\n"; }

command_exists() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# 1. Misc system packages (incl. zstd for the dataset tarball).
# ---------------------------------------------------------------------------
setup_misc() {
  header "Misc packages"
  PKGS=()
  command_exists git   || PKGS+=(git)
  command_exists curl  || PKGS+=(curl)
  command_exists jq    || PKGS+=(jq)
  command_exists tmux  || PKGS+=(tmux)
  command_exists zstd  || PKGS+=(zstd)
  command_exists tar   || PKGS+=(tar)

  if [ ${#PKGS[@]} -gt 0 ]; then
    info "Installing: ${PKGS[*]}"
    apt-get update -qq || warn "apt-get update failed (continuing)"
    apt-get install -y -qq "${PKGS[@]}" || warn "Some misc packages failed to install"
  fi
  ok "git curl jq tmux zstd tar present (best-effort)"
}

# ---------------------------------------------------------------------------
# 2. Docker CLI — client only; daemon lives on the COS host (DooD).
# ---------------------------------------------------------------------------
setup_docker_cli() {
  header "Docker CLI (DooD)"

  if command_exists docker; then
    ok "docker CLI already present ($(docker --version))"
  else
    info "Installing docker-ce-cli (CLI only — uses host daemon via socket)..."
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg lsb-release

    mkdir -p /etc/apt/keyrings
    DOCKER_DISTRO=$(. /etc/os-release && echo "${ID:-debian}")
    curl -fsSL "https://download.docker.com/linux/${DOCKER_DISTRO}/gpg" \
      | gpg --dearmor -o /etc/apt/keyrings/docker.gpg

    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/${DOCKER_DISTRO} $(lsb_release -cs) stable" \
      | tee /etc/apt/sources.list.d/docker.list >/dev/null

    apt-get update -qq

    # Pin to 24.x for client/server protocol compatibility with the COS host
    # Docker 24.0.x daemon (same rationale as swe-auto-eval).
    CLI_24=$(apt-cache madison docker-ce-cli 2>/dev/null \
      | awk -F'[| ]+' '$3~/^5:24\./{print $3;exit}')
    if [ -n "$CLI_24" ]; then
      apt-get install -y -qq --allow-downgrades "docker-ce-cli=${CLI_24}"
    else
      warn "docker-ce-cli 24.x not found in apt; installing latest"
      apt-get install -y -qq docker-ce-cli
    fi
    ok "docker-ce-cli installed ($(docker --version))"
  fi

  if [ -S /var/run/docker.sock ]; then
    export DOCKER_HOST="unix:///var/run/docker.sock"
    if timeout 10 docker info >/dev/null 2>&1; then
      SERVER_VER=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "?")
      ok "Connected to host Docker daemon (server $SERVER_VER) via /var/run/docker.sock"
    else
      warn "Docker socket present but daemon not responding — check the Batch job's volume mount"
    fi
  else
    warn "/var/run/docker.sock not found — Batch job must bind-mount it (DooD)"
  fi
}

# ---------------------------------------------------------------------------
# 3. gcloud + Artifact Registry auth.
#    Mirrors swe-auto-eval setup.sh install_docker() lines ~1059-1156
#    line-for-line: tarball install under /var/lib/docker (COS read-only root),
#    configure-docker credential helper, metadata-token docker login, and
#    persist env to /var/lib/docker/gcloud-env.sh so run.sh inherits it.
# ---------------------------------------------------------------------------
setup_gcloud_gar_auth() {
  header "gcloud + Artifact Registry auth"

  case "$(uname -s)" in
    Linux) : ;;
    *)
      warn "Non-Linux host — skipping gcloud/GAR auth (local dev: ensure"
      warn "you can 'docker pull' from GAR yourself, or run with --no-gar)."
      return 0
      ;;
  esac

  GCLOUD_INSTALL_DIR="/var/lib/docker/google-cloud-sdk"
  GCLOUD_BIN="${GCLOUD_INSTALL_DIR}/bin"
  GCLOUD_CONFIG_DIR="/var/lib/docker/gcloud-config"
  DOCKER_CONFIG_DIR="/var/lib/docker/docker-config"
  GAR_HOST="us-central1-docker.pkg.dev"

  if [ ! -x "${GCLOUD_BIN}/gcloud" ]; then
    info "Installing Google Cloud SDK from tarball (apt not available on COS)..."
    GCLOUD_TGZ="/var/lib/docker/google-cloud-cli-linux-x86_64.tar.gz"
    if curl -fsSL \
        "https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-linux-x86_64.tar.gz" \
        -o "${GCLOUD_TGZ}"; then
      tar -xf "${GCLOUD_TGZ}" -C /var/lib/docker/ || warn "Failed to extract gcloud tarball"
      rm -f "${GCLOUD_TGZ}"
    else
      warn "Failed to download Google Cloud SDK tarball"
    fi
  else
    info "Google Cloud SDK already installed at ${GCLOUD_INSTALL_DIR}"
  fi

  if [ -x "${GCLOUD_BIN}/gcloud" ]; then
    ok "gcloud binary found at ${GCLOUD_BIN}/gcloud"
    export CLOUDSDK_CONFIG="${GCLOUD_CONFIG_DIR}"
    export DOCKER_CONFIG="${DOCKER_CONFIG_DIR}"
    mkdir -p "${GCLOUD_CONFIG_DIR}" "${DOCKER_CONFIG_DIR}"
    export PATH="${GCLOUD_BIN}:${PATH}"

    info "Configuring Docker to use the gcloud credential helper for GAR..."
    "${GCLOUD_BIN}/gcloud" auth configure-docker "${GAR_HOST}" --quiet 2>/dev/null \
      && ok "gcloud Docker credential helper configured" \
      || warn "gcloud auth configure-docker failed"
  else
    warn "gcloud not found after install attempt — relying on metadata-token auth"
  fi

  # Primary auth: VM service-account token from the metadata server. Always
  # available on Batch VMs regardless of gcloud install status.
  info "Authenticating Docker to GAR via VM service account (metadata server)..."
  METADATA_TOKEN=$(curl -s -H "Metadata-Flavor: Google" \
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" 2>/dev/null \
    | python3 -c "import sys,json;print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null || true)

  if [ -n "${METADATA_TOKEN}" ]; then
    echo "${METADATA_TOKEN}" | docker login -u oauth2accesstoken --password-stdin \
      "https://${GAR_HOST}" 2>/dev/null \
      && ok "Docker authenticated to GAR via metadata token" \
      || warn "docker login with metadata token failed"
  else
    warn "Could not retrieve metadata token (not on a GCP VM?) — GAR pulls may fail"
  fi

  # Persist for run.sh (sourced there, same contract as swe-auto-eval).
  GENV="/var/lib/docker/gcloud-env.sh"
  if [ -d /var/lib/docker ] && [ -w /var/lib/docker ]; then
    {
      [ -n "${DOCKER_CONFIG:-}" ]   && echo "export DOCKER_CONFIG=${DOCKER_CONFIG}"
      [ -n "${CLOUDSDK_CONFIG:-}" ] && echo "export CLOUDSDK_CONFIG=${CLOUDSDK_CONFIG}"
      [ -x "${GCLOUD_BIN}/gcloud" ] && echo "export PATH=${GCLOUD_BIN}:\${PATH}"
    } > "${GENV}" 2>/dev/null \
      && ok "gcloud/docker env written to ${GENV}" \
      || warn "could not write ${GENV}"
  else
    warn "/var/lib/docker not writable — run.sh will refresh GAR auth itself"
  fi
}

# ---------------------------------------------------------------------------
# 4. Python 3.11+ (required by uv and harbor).
# ---------------------------------------------------------------------------
setup_python() {
  header "Python 3.11+"
  PYTHON_BIN=""
  for py in python3.13 python3.12 python3.11 python3; do
    if command_exists "$py"; then
      MAJOR=$("$py" -c 'import sys;print(sys.version_info.major)' 2>/dev/null || echo 0)
      MINOR=$("$py" -c 'import sys;print(sys.version_info.minor)' 2>/dev/null || echo 0)
      if [ "$MAJOR" -eq 3 ] && [ "$MINOR" -ge 11 ]; then
        PYTHON_BIN="$py"; ok "Found $("$py" --version) at $(command -v "$py")"; break
      fi
    fi
  done
  if [ -z "$PYTHON_BIN" ]; then
    info "No Python 3.11+ found. Installing via deadsnakes PPA..."
    apt-get update -qq
    apt-get install -y -qq software-properties-common
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -qq
    apt-get install -y -qq python3.11 python3.11-venv python3.11-dev
    PYTHON_BIN="python3.11"; ok "Installed Python 3.11"
  fi
  export PYTHON_BIN
}

# ---------------------------------------------------------------------------
# 5. uv (manages the isolated harbor install).
# ---------------------------------------------------------------------------
setup_uv() {
  header "uv"
  export PATH="$HOME/.local/bin:$PATH"
  if command_exists uv; then ok "uv already installed ($(uv --version))"; return; fi
  info "Installing uv..."
  curl -fsSL https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  command_exists uv && ok "uv installed ($(uv --version))" || die "uv installation failed"
}

# ---------------------------------------------------------------------------
# 6. harbor + xyne adapter (single isolated uv tool env).
# ---------------------------------------------------------------------------
setup_harbor() {
  header "harbor + xyne adapter"
  export PATH="$HOME/.local/bin:$PATH"

  ADAPTER_DIR="${SCRIPT_DIR}/adapter"
  if [ -d "$ADAPTER_DIR" ]; then
    info "Installing harbor with the xyne adapter editable in its env..."
    if uv tool install harbor --with-editable "$ADAPTER_DIR" --reinstall; then
      ok "harbor installed with xyne_harbor_agent available"
    else
      warn "harbor+adapter install failed; retrying harbor alone then injecting adapter"
      uv tool install harbor --reinstall || die "harbor install failed"
      uv tool run --from harbor python -m pip install -e "$ADAPTER_DIR" \
        || warn "Could not inject adapter — xyne-cli agent may be unavailable"
    fi
  else
    warn "adapter/ not found — installing harbor without xyne-cli support"
    uv tool install harbor --reinstall || die "harbor install failed"
  fi
  command_exists harbor && ok "harbor: $(harbor --version 2>/dev/null || echo installed)" \
    || die "harbor not on PATH after install"
}

# ---------------------------------------------------------------------------
# 7. Helper-script Python deps (PyYAML for config parsing, etc.).
# ---------------------------------------------------------------------------
setup_python_deps() {
  header "Helper-script dependencies"
  REQ="${SCRIPT_DIR}/requirements.txt"
  [ -f "$REQ" ] || { warn "requirements.txt missing — skipping"; return; }
  if command_exists uv; then
    uv pip install --system -r "$REQ" 2>/dev/null \
      || pip3 install -r "$REQ" 2>/dev/null \
      || pip install -r "$REQ" 2>/dev/null \
      || warn "Could not install helper deps; scripts use a built-in YAML fallback"
  else
    pip3 install -r "$REQ" 2>/dev/null || pip install -r "$REQ" 2>/dev/null \
      || warn "Could not install helper deps; scripts use a built-in YAML fallback"
  fi
  python3 -c "import yaml" 2>/dev/null && ok "PyYAML available" \
    || warn "PyYAML unavailable — helper scripts fall back to a minimal parser"
}

# ---------------------------------------------------------------------------
# 8. Dataset (harbor task packages) — restore from GCS tarball.
# ---------------------------------------------------------------------------
setup_dataset() {
  header "terminal-bench dataset"
  export PATH="$HOME/.local/bin:$PATH"   # harbor on PATH for the fallback
  if bash "${SCRIPT_DIR}/scripts/fetch_dataset_tarball.sh"; then
    ok "Dataset ready"
  else
    warn "Dataset fetch reported a problem — run.sh will re-check before running"
  fi
}

# ---------------------------------------------------------------------------
# 9. xyne-cli binaries — pulled from the npm registry as platform packages.
#
# As of @xyne/xyne-cli >= 0.1.2 the main package's tarball NO LONGER ships
# the prebuilt linux binaries. They live in per-platform sibling packages
# (the esbuild / @swc/core pattern), declared as `optionalDependencies` on
# the main package and version-locked to it:
#
#   @xyne/xyne-cli@X.Y.Z
#     optionalDependencies:
#       @xyne/xyne-cli-linux-x64   @ X.Y.Z   ← binary at package/xyne-linux-x64
#       @xyne/xyne-cli-linux-arm64 @ X.Y.Z   ← binary at package/xyne-linux-arm64
#       @xyne/xyne-cli-darwin-*    @ X.Y.Z
#       @xyne/xyne-cli-win32-x64   @ X.Y.Z
#
# The binary sits at the **top of the sibling tarball** as
# `package/xyne-linux-<arch>` (NOT under `binaries/`). The main package is
# still where `package.json` comes from — `XyneCliAgent.install()` reads it
# next to the binary, so we still need it locally.
#
# Steps below:
#   1. Resolve the main package's `latest` version.
#   2. For each desired platform, fetch `<pkg>-<plat>@<version>`, extract
#      `package/xyne-<plat>` → `${LOCAL_DIR}/xyne-<plat>`.
#   3. Fetch main package.json → `${LOCAL_DIR}/package.json`.
# ---------------------------------------------------------------------------
setup_xyne_binaries() {
  header "xyne-cli binaries (platform packages from npm)"

  LOCAL_DIR="${SCRIPT_DIR}/binaries"
  PKG=$(python3 - "${SCRIPT_DIR}/config.yaml" <<'PY' 2>/dev/null || echo "@xyne/xyne-cli"
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
print((cfg.get("xyne_binary") or {}).get("npm_package", "@xyne/xyne-cli"))
PY
)

  # Idempotent + local-dev-friendly: if the dir is already populated (incl.
  # via the repo's `binaries -> ../xyne-cli/binaries` symlink on a dev box),
  # skip the fetch.
  if [ -s "${LOCAL_DIR}/xyne-linux-x64" ] && [ -s "${LOCAL_DIR}/package.json" ]; then
    ok "xyne binaries already present at ${LOCAL_DIR}"
    return 0
  fi

  # On a fresh Batch VM the `binaries` repo symlink points at a sibling that
  # doesn't exist (broken link). Replace it with a real dir before fetching.
  if [ -L "${LOCAL_DIR}" ] && [ ! -e "${LOCAL_DIR}" ]; then
    info "Removing broken symlink ${LOCAL_DIR}"
    rm -f "${LOCAL_DIR}"
  fi
  mkdir -p "${LOCAL_DIR}"

  # Resolve latest version of the main package — sibling packages are
  # version-locked to it via optionalDependencies.
  info "Resolving latest version of ${PKG} from npm…"
  PKG_VERSION=$(curl -fsSL "https://registry.npmjs.org/${PKG}/latest" 2>/dev/null \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["version"])' 2>/dev/null)
  if [ -z "${PKG_VERSION}" ]; then
    warn "Could not resolve ${PKG} latest version — xyne-cli agent will be unavailable"
    warn "(claude-code / opencode / pi / aider still work.)"
    return 0
  fi
  ok "Latest ${PKG}: ${PKG_VERSION}"

  # Fetch each linux platform sibling. URL-encode the '/' in the scoped name
  # so curl talks to /<scope>%2F<pkg>/<version>.
  any=0
  for plat in linux-x64 linux-arm64; do
    SIB="${PKG}-${plat}"
    SIB_URL="https://registry.npmjs.org/${SIB//\//%2F}/${PKG_VERSION}"
    SIB_TARBALL=$(curl -fsSL "${SIB_URL}" 2>/dev/null \
      | python3 -c 'import sys,json; print(json.load(sys.stdin)["dist"]["tarball"])' 2>/dev/null)
    if [ -z "${SIB_TARBALL}" ]; then
      warn "  ${SIB}@${PKG_VERSION} not found on npm — skipping ${plat}"
      continue
    fi

    TMP_DIR="$(mktemp -d)"
    TMP_TAR="${TMP_DIR}/sib.tgz"
    info "Downloading ${SIB_TARBALL}"
    if ! curl -fsSL "${SIB_TARBALL}" -o "${TMP_TAR}"; then
      warn "  Failed to download ${SIB}@${PKG_VERSION} — skipping ${plat}"
      rm -rf "${TMP_DIR}"; continue
    fi
    # Selective extract — sibling tarballs are 50-60 MB and we only want one file.
    if ! tar -xzf "${TMP_TAR}" -C "${TMP_DIR}" "package/xyne-${plat}" 2>/dev/null; then
      warn "  ${SIB}@${PKG_VERSION} tarball missing package/xyne-${plat} — skipping"
      rm -rf "${TMP_DIR}"; continue
    fi
    cp "${TMP_DIR}/package/xyne-${plat}" "${LOCAL_DIR}/xyne-${plat}"
    chmod +x "${LOCAL_DIR}/xyne-${plat}"
    ok "  -> ${LOCAL_DIR}/xyne-${plat}"
    any=1
    rm -rf "${TMP_DIR}"
  done

  # XyneCliAgent.install() requires package.json next to the binary; fetch it
  # from the main package tarball (selective extract — we don't want the rest).
  MAIN_TARBALL=$(curl -fsSL "https://registry.npmjs.org/${PKG//\//%2F}/${PKG_VERSION}" 2>/dev/null \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["dist"]["tarball"])' 2>/dev/null)
  if [ -n "${MAIN_TARBALL}" ]; then
    TMP_DIR="$(mktemp -d)"
    TMP_TAR="${TMP_DIR}/main.tgz"
    info "Downloading ${MAIN_TARBALL} (for package.json)"
    if curl -fsSL "${MAIN_TARBALL}" -o "${TMP_TAR}" \
       && tar -xzf "${TMP_TAR}" -C "${TMP_DIR}" package/package.json 2>/dev/null; then
      cp "${TMP_DIR}/package/package.json" "${LOCAL_DIR}/package.json"
      ok "  -> ${LOCAL_DIR}/package.json"
    else
      warn "  Could not fetch main package.json for ${PKG}@${PKG_VERSION}"
    fi
    rm -rf "${TMP_DIR}"
  else
    warn "  Could not resolve main tarball URL for ${PKG}@${PKG_VERSION}"
  fi

  if [ "${any}" = 1 ]; then
    ok "xyne-cli binaries fetched (${PKG}@${PKG_VERSION}, platform packages)"
  else
    warn "No xyne-cli linux binaries fetched — only xyne-cli agent affected"
  fi
}

# ---------------------------------------------------------------------------
# 10. .env template (don't clobber secrets).
# ---------------------------------------------------------------------------
write_env_file() {
  header "Environment file"
  ENV_FILE="${SCRIPT_DIR}/.env"
  if [ -f "$ENV_FILE" ]; then
    info ".env already exists — leaving unchanged"
  else
    cat > "$ENV_FILE" <<'ENVEOF'
# terminal-bench environment — edit before running run.sh manually.
# (On the Batch VM the eval-runner passes the key as the first positional arg.)
#
#   XYNE_API_KEY — grid.ai API key (used by every agent)
#   XYNE_BASE_URL — grid.ai endpoint (default https://grid.ai.juspay.net/v1)
# export XYNE_API_KEY=sk-...
ENVEOF
    warn ".env created from template — set XYNE_API_KEY before manual run.sh"
  fi
  if [ -n "${DOCKER_HOST:-}" ] && ! grep -q "^export DOCKER_HOST" "$ENV_FILE" 2>/dev/null; then
    echo "export DOCKER_HOST=${DOCKER_HOST}" >> "$ENV_FILE"
  fi
}

# ---------------------------------------------------------------------------
# 11. CLI PATH helper (sourced by run.sh) — mirrors swe-auto-eval.
# ---------------------------------------------------------------------------
create_cli_path_helper() {
  header "CLI PATH helper"
  cat > "${SCRIPT_DIR}/.cli_paths.sh" <<'EOF'
#!/bin/bash
# Auto-generated by setup.sh — ensures harbor + agent CLIs are on PATH.
CLI_PATHS=(
  "$HOME/.local/bin"
  "$HOME/.local/share/uv/tools/harbor/bin"
  "$HOME/.opencode/bin"
  "$HOME/.bun/bin"
  "$HOME/bin"
  "/usr/local/bin"
  "/var/lib/docker/google-cloud-sdk/bin"
)
for p in "${CLI_PATHS[@]}"; do
  if [ -d "$p" ] && [[ ":$PATH:" != *":$p:"* ]]; then export PATH="$p:$PATH"; fi
done
EOF
  chmod +x "${SCRIPT_DIR}/.cli_paths.sh"
  ok "Wrote ${SCRIPT_DIR}/.cli_paths.sh"
}

# ---------------------------------------------------------------------------
# 12. Verification summary.
# ---------------------------------------------------------------------------
verify() {
  header "Verification"
  all_ok=true
  check() {
    if eval "$2" >/dev/null 2>&1; then ok "$1"; else warn "MISSING: $1"; all_ok=false; fi
  }
  check "docker CLI"            "command_exists docker"
  check "docker daemon"         "timeout 5 docker info"
  check "uv"                    "command_exists uv"
  check "harbor"                "command_exists harbor"
  check "xyne adapter import"   "uv tool run --from harbor python -c 'import xyne_harbor_agent.agent'"
  check "dataset cache"         "[ -n \"\$(find \$HOME/.cache/harbor -name task.toml 2>/dev/null | head -1)\" ]"
  check "xyne linux binary"     "[ -s \"${SCRIPT_DIR}/binaries/xyne-linux-x64\" ] || [ -s \"${SCRIPT_DIR}/binaries/xyne-linux-arm64\" ]"
  check "gcloud-env.sh"         "[ -f /var/lib/docker/gcloud-env.sh ] || [ \"\$(uname -s)\" != Linux ]"
  check "run.sh executable"     "[ -x \"${SCRIPT_DIR}/run.sh\" ]"
  check "git"                   "command_exists git"
  check "jq"                    "command_exists jq"
  check "zstd"                  "command_exists zstd"

  if [ "$all_ok" = true ]; then ok "All checks passed"; else warn "Some checks failed — review above"; fi

  echo
  info "Next steps:"
  info "  Smoke (manual):  export XYNE_API_KEY=sk-...; ./run.sh smoke-run --task regex-log --agent xyne-cli --model private-large"
  info "  Batch:           the eval-runner invokes ./run.sh [API_KEY] EVAL_RUN_ID [--flags]"
}

# ---------------------------------------------------------------------------
# Main — order matters: docker+gcloud auth before harbor/dataset so any
# GAR-backed step has working credentials.
# ---------------------------------------------------------------------------
main() {
  echo -e "${BLUE}"
  echo "╔══════════════════════════════════════════════════════════════╗"
  echo "║   terminal-bench setup (harbor + grid.ai + GAR, Batch VM)    ║"
  echo "╚══════════════════════════════════════════════════════════════╝"
  echo -e "${NC}"

  setup_misc
  setup_docker_cli
  setup_gcloud_gar_auth
  setup_python
  setup_uv
  setup_harbor
  setup_python_deps
  setup_dataset
  setup_xyne_binaries
  write_env_file
  create_cli_path_helper
  verify
}

main "$@"
