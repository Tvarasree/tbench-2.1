#!/usr/bin/env bash
# =============================================================================
# terminal-bench runner — harbor harness, grid.ai (juspay) models, task images
# served from Google Artifact Registry. Built to the eval-runner harness
# contract so it drops into the same Batch VM pipeline as swe-auto-eval.
#
# INVOCATION CONTRACT (identical shape to swe-auto-eval/run.sh):
#   ./run.sh [API_KEY] EVAL_RUN_ID [--flag value ...]
#     * API_KEY   — optional; the eval-runner prepends it only when
#                   grid_ai_api_key is configured. Omit it for manual runs
#                   (export XYNE_API_KEY instead).
#     * EVAL_RUN_ID — required; first non-api-key positional. The results file
#                   is written as <output>/<EVAL_RUN_ID>_results.json, which is
#                   exactly what the runner reads back.
#     * remaining args are named flags derived from input_param.json (the
#                   eval-runner already translated JSON keys '_' -> '-').
#
# Manual back-compat: if the first arg is a flag (starts with --), a timestamp
# run id is synthesised so `./run.sh --task regex-log` still works locally.
#
# ALWAYS writes a standardized results JSON (even on partial/empty failure) so
# the harness can submit metrics instead of marking the run FAILED.
# =============================================================================

# Re-exec under stdbuf so all descendants are line-buffered (host log_sync
# freshness). Guard prevents a re-exec loop. exec `bash "$0"` (not `"$0"`):
# stdbuf resolves a slash-less argv[0] via $PATH not cwd, so plain
# `exec stdbuf "$0"` breaks under `bash run.sh ...`. bash is always on PATH
# and resolves "$0" from cwd — works for every invocation style.
if [ -z "${STDBUF_APPLIED:-}" ] && command -v stdbuf &>/dev/null; then
  export STDBUF_APPLIED=1
  exec stdbuf -oL -eL bash "$0" "$@"
fi

# NOT `set -e`: a single task/grade failure must not abort before we write
# results.json. Errors are handled explicitly.
set -uo pipefail
export PYTHONUNBUFFERED=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
# shellcheck disable=SC1091
. "${SCRIPT_DIR}/scripts/heartbeat.sh"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log_info()    { echo -e "${BLUE}[info]  $*${NC}"; }
log_ok()      { echo -e "${GREEN}[ok]    $*${NC}"; }
log_warn()    { echo -e "${YELLOW}[warn]  $*${NC}"; }
log_err()     { echo -e "${RED}[error] $*${NC}" >&2; }
log_step()    { echo -e "\n${BLUE}══ $* ══${NC}\n"; }

# Inherit gcloud/Artifact-Registry auth written by setup.sh, and CLI PATHs.
[ -f /var/lib/docker/gcloud-env.sh ] && { . /var/lib/docker/gcloud-env.sh || true; }
[ -f "${SCRIPT_DIR}/.cli_paths.sh" ] && { . "${SCRIPT_DIR}/.cli_paths.sh" || true; }
export PATH="$HOME/.local/bin:$PATH"
# .env only for manual runs; never override an already-exported key.
if [ -f "${SCRIPT_DIR}/.env" ]; then
  # shellcheck disable=SC1091
  set -a; . "${SCRIPT_DIR}/.env" 2>/dev/null || true; set +a
fi

print_usage() {
  cat <<'EOF'
Usage: ./run.sh [API_KEY] EVAL_RUN_ID [OPTIONS]

Positional:
  API_KEY        Optional; prepended by the eval-runner when grid_ai_api_key set.
  EVAL_RUN_ID    Required; names the results file <output>/<id>_results.json.

Selection (mutually exclusive; default: --task regex-log):
  --task NAME            Single task
  --range START-END      0-indexed inclusive slice of the sorted task list
  --limit N              First N tasks
  --all                  Every task in the dataset

Model & agent:
  --model NAME           grid.ai model id (default: private-large)
  --coding-agent NAME    Agent: xyne-cli (default), claude-code, opencode,
                         pi, aider, goose, codex   (alias: --agent)
  --base-url URL         API base (default: https://grid.ai.juspay.net/v1)
  --concurrency N        harbor --n-concurrent (default: 1)
  --attempts N           harbor --n-attempts / pass@k (default: 3)

Token pricing (all USD per 1,000,000 tokens; optional):
  --price-input RATE     Input-token rate. Both input and output are required
                         for a priced report; one alone leaves it unpriced.
  --price-output RATE    Output-token rate.
  --price-cached RATE    Cached-read rate. Omit and cache reads bill at the
                         input rate (overstates cost when caching is active).
  --price-cache-write RATE
                         Cache-write rate. Omit and cache writes bill at the
                         input rate.

Misc:
  --agent-timeout MULT   Scale per-task agent timeout (harbor multiplier)
  --dataset NAME         Harbor dataset (default: terminal-bench/terminal-bench-2-1)
  --no-gar               Skip the GAR pre-pull (let harbor pull from Docker Hub)
  --log-level LEVEL      Accepted for schema parity; unused
  --help                 This message

Environment:
  XYNE_API_KEY  grid.ai key (used by every agent). Provided via positional
                API_KEY on the Batch VM, or exported for manual runs.
EOF
}

# ---------------------------------------------------------------------------
# Positional: [API_KEY] EVAL_RUN_ID   (mirrors swe-auto-eval lines 147-162)
# ---------------------------------------------------------------------------
API_KEY=""
EVAL_RUN_ID=""
if [ $# -ge 1 ] && [[ "$1" != --* ]]; then
  if [ $# -ge 2 ] && [[ "$2" != --* ]]; then
    API_KEY="$1"; EVAL_RUN_ID="$2"; shift 2
  else
    EVAL_RUN_ID="$1"; shift
  fi
else
  # Manual back-compat: no positional run id → synthesise one.
  EVAL_RUN_ID="local_$(date +%Y%m%d_%H%M%S)"
fi

# ---------------------------------------------------------------------------
# Defaults (align with the input_param.json schema defaults).
# ---------------------------------------------------------------------------
MODEL="private-large"
AGENT="xyne-cli"
BASE_URL="https://grid.ai.juspay.net/v1"
DATASET="terminal-bench/terminal-bench-2-1"
CONCURRENCY="1"
ATTEMPTS="3"
AGENT_TIMEOUT_MULT=""
TASK_SELECTOR=""
SELECTOR_KIND="task"
USE_GAR=1
LOG_LEVEL="info"
# Token pricing, USD per 1,000,000 tokens. Empty = not supplied. Kept as
# strings so "unset" stays distinguishable from a deliberate 0.
PRICE_INPUT=""
PRICE_OUTPUT=""
PRICE_CACHED=""
PRICE_CACHE_WRITE=""

# ---------------------------------------------------------------------------
# Named-flag parser. Known flags → vars; unknown → forwarded to harbor as-is
# (best-effort pass-through, mirroring swe-auto-eval's contract).
# ---------------------------------------------------------------------------
EXTRA_HARBOR_FLAGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --task)            TASK_SELECTOR="$2"; SELECTOR_KIND="task";  shift 2 ;;
    --range)           TASK_SELECTOR="$2"; SELECTOR_KIND="range"; shift 2 ;;
    --limit)           TASK_SELECTOR="$2"; SELECTOR_KIND="limit"; shift 2 ;;
    --all)             TASK_SELECTOR="";   SELECTOR_KIND="all";   shift   ;;
    --model)           MODEL="$2";              shift 2 ;;
    --coding-agent|--agent) AGENT="$2";         shift 2 ;;
    --base-url)        BASE_URL="$2";           shift 2 ;;
    --concurrency)     CONCURRENCY="$2";        shift 2 ;;
    --attempts)        ATTEMPTS="$2";           shift 2 ;;
    --dataset)         DATASET="$2";            shift 2 ;;
    --agent-timeout)   AGENT_TIMEOUT_MULT="$2"; shift 2 ;;
    --no-gar)          USE_GAR=0;               shift   ;;
    --price-input)     PRICE_INPUT="$2";        shift 2 ;;
    --price-output)    PRICE_OUTPUT="$2";       shift 2 ;;
    --price-cached)    PRICE_CACHED="$2";       shift 2 ;;
    --price-cache-write) PRICE_CACHE_WRITE="$2"; shift 2 ;;
    --log-level)       LOG_LEVEL="$2";          shift 2 ;;   # schema parity
    --help|-h)         print_usage; exit 0 ;;
    --*)
      # Unknown flag: forward to harbor run (value if present).
      if [ $# -ge 2 ] && [[ "$2" != --* ]]; then
        EXTRA_HARBOR_FLAGS+=("$1" "$2"); shift 2
      else
        EXTRA_HARBOR_FLAGS+=("$1"); shift
      fi
      ;;
    *) log_warn "ignoring unexpected positional: $1"; shift ;;
  esac
done

[ "$SELECTOR_KIND" = "task" ] && [ -z "$TASK_SELECTOR" ] && TASK_SELECTOR="regex-log"

# ---------------------------------------------------------------------------
# Output location: honor the eval-runner's injected dir, else <repo>/output
# (identical to swe-auto-eval's RUNNER_OUTPUT_ROOT default).
# ---------------------------------------------------------------------------
OUTPUT_DIR="${EVAL_RUNNER_OUTPUT_DIR:-${SCRIPT_DIR}/output}"
mkdir -p "$OUTPUT_DIR"
RESULTS_FILE="${OUTPUT_DIR}/${EVAL_RUN_ID}_results.json"

# Always-write guard: if we exit for ANY reason without a results file, drop a
# zero-metric one so the harness submits metrics instead of marking FAILED.
write_fallback_results() {
  [ -f "$RESULTS_FILE" ] && return 0
  local reason="${1:-unknown}"
  cat > "$RESULTS_FILE" <<JSON
{
  "metrics": {
    "main": { "name": "Solved", "value": 0 },
    "secondary": { "solved": 0, "total": 0, "solve_rate_pct": 0,
                   "successful_trials": 0, "planned_trials": 0,
                   "trial_accuracy_pct": 0 },
    "additional": { "status": "no-results", "reason": "${reason}" }
  }
}
JSON
  log_warn "Wrote fallback zero-metric results ($reason) → $RESULTS_FILE"
}

HB_PID=""
stop_heartbeat() {
  [ -n "$HB_PID" ] || return 0
  kill "$HB_PID" 2>/dev/null || true
  wait "$HB_PID" 2>/dev/null || true
  HB_PID=""
}

cleanup_on_exit() {
  local rc=$?
  trap - EXIT
  stop_heartbeat
  write_fallback_results "interrupted-or-error (exit $rc)"
  exit "$rc"
}
trap cleanup_on_exit EXIT

# ---------------------------------------------------------------------------
# API key. The positional wins; else an exported XYNE_API_KEY (manual).
# ---------------------------------------------------------------------------
if [ -n "$API_KEY" ]; then
  export XYNE_API_KEY="$API_KEY"
fi
if [ -z "${XYNE_API_KEY:-}" ]; then
  log_err "No API key. Pass it as the first positional arg or export XYNE_API_KEY."
  exit 1
fi
# Export under every convention an agent may read. This block mirrors
# swe-auto-eval/run.sh's key export (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN /
# GRID_AI_API_KEY / OPENAI_API_KEY) plus the xyne-specific vars our adapter
# needs. ANTHROPIC_AUTH_TOKEN is the critical one: claude-code routed through
# grid.ai authenticates with the bearer AUTH_TOKEN, not the x-api-key that the
# Anthropic SDK sends for ANTHROPIC_API_KEY. (LITE_LLM_API_KEY is deliberately
# omitted — LiteLLM is swe-auto-eval-specific and unused by harbor.)
export XYNE_API_KEY
export XYNE_BASE_URL="${BASE_URL}"
export GRID_AI_API_KEY="${XYNE_API_KEY}"
export OPENAI_API_KEY="${XYNE_API_KEY}"
export OPENAI_BASE_URL="${BASE_URL}"
export ANTHROPIC_API_KEY="${XYNE_API_KEY}"
export ANTHROPIC_AUTH_TOKEN="${XYNE_API_KEY}"
export ANTHROPIC_BASE_URL="${BASE_URL%/v1}"   # SDK appends /v1 itself

# ---------------------------------------------------------------------------
# Docker socket (DooD). Honor an existing DOCKER_HOST, else autodetect.
# ---------------------------------------------------------------------------
if [ -z "${DOCKER_HOST:-}" ]; then
  if [ -S /var/run/docker.sock ]; then
    export DOCKER_HOST="unix:///var/run/docker.sock"
  elif SOCK=$(podman machine inspect --format '{{.ConnectionInfo.PodmanSocket.Path}}' 2>/dev/null); then
    export DOCKER_HOST="unix://$SOCK"
  fi
fi

# ---------------------------------------------------------------------------
# Dataset cache — defensively restore if setup.sh's fetch didn't land.
# ---------------------------------------------------------------------------
DATASET_ORG="${DATASET%%/*}"
DATASET_CACHE="$HOME/.cache/harbor/tasks/packages/${DATASET_ORG}"
if [ ! -d "$DATASET_CACHE" ] || [ -z "$(ls -A "$DATASET_CACHE" 2>/dev/null)" ]; then
  log_warn "Task cache for '$DATASET_ORG' missing — attempting restore…"
  bash "${SCRIPT_DIR}/scripts/fetch_dataset_tarball.sh" || true
fi
if [ ! -d "$DATASET_CACHE" ] || [ -z "$(ls -A "$DATASET_CACHE" 2>/dev/null)" ]; then
  log_err "No tasks for org '$DATASET_ORG' at $DATASET_CACHE — cannot run."
  exit 1
fi

# ---------------------------------------------------------------------------
# Build sorted task list, apply selector.
# ---------------------------------------------------------------------------
mapfile -t ALL_TASKS < <(ls "$DATASET_CACHE" | sort)
N_ALL=${#ALL_TASKS[@]}

select_tasks() {
  case "$SELECTOR_KIND" in
    all)  printf '%s\n' "${ALL_TASKS[@]}" ;;
    task)
      local f=0
      for t in "${ALL_TASKS[@]}"; do [ "$t" = "$TASK_SELECTOR" ] && { f=1; break; }; done
      [ "$f" = 1 ] || { log_err "task '$TASK_SELECTOR' not in dataset cache"; exit 1; }
      echo "$TASK_SELECTOR" ;;
    range)
      local s="${TASK_SELECTOR%-*}" e="${TASK_SELECTOR#*-}"
      if ! [[ "$s" =~ ^[0-9]+$ && "$e" =~ ^[0-9]+$ ]] || [ "$s" -gt "$e" ]; then
        log_err "invalid --range '$TASK_SELECTOR' (want START-END inclusive)"; exit 1
      fi
      [ "$e" -ge "$N_ALL" ] && e=$((N_ALL - 1))
      for i in $(seq "$s" "$e"); do printf '%s\n' "${ALL_TASKS[$i]}"; done ;;
    limit)
      [[ "$TASK_SELECTOR" =~ ^[0-9]+$ ]] || { log_err "--limit must be an integer"; exit 1; }
      local n="$TASK_SELECTOR"; [ "$n" -gt "$N_ALL" ] && n="$N_ALL"
      for i in $(seq 0 $((n - 1))); do printf '%s\n' "${ALL_TASKS[$i]}"; done ;;
  esac
}
mapfile -t SELECTED < <(select_tasks)
N_SEL=${#SELECTED[@]}
[ "$N_SEL" -ge 1 ] || { log_err "selection produced 0 tasks"; exit 1; }

log_step "selection"
echo "    eval_run_id: $EVAL_RUN_ID"
echo "    dataset:     $DATASET ($N_ALL tasks total)"
echo "    selector:    $SELECTOR_KIND ${TASK_SELECTOR:-(all)}"
echo "    selected:    $N_SEL task(s)"
echo "    agent:       $AGENT"
echo "    model:       $MODEL"
echo "    concurrency: $CONCURRENCY"
echo "    attempts:    $ATTEMPTS"
echo "    agent-timeout: $([ -n "$AGENT_TIMEOUT_MULT" ] && echo "${AGENT_TIMEOUT_MULT}x (harbor --agent-timeout-multiplier)" || echo "1.0x (each task's task.toml default)")"
echo "    base-url:    $BASE_URL"
echo "    gar pre-pull: $([ "$USE_GAR" = 1 ] && echo yes || echo no)"
echo "    pricing:     $( [ -n "$PRICE_INPUT" ] && [ -n "$PRICE_OUTPUT" ] \
  && echo "in=\$${PRICE_INPUT} out=\$${PRICE_OUTPUT} cache-read=\$${PRICE_CACHED:-<input rate>} cache-write=\$${PRICE_CACHE_WRITE:-<input rate>} per 1M tokens" \
  || echo "none (token counts reported unpriced)")"
echo "    output:      $RESULTS_FILE"

# ---------------------------------------------------------------------------
# GAR pre-pull — make selected task images present under the harbor-expected
# name so harbor never reaches Docker Hub. Non-fatal: harbor would otherwise
# fall back to its declared Docker Hub image.
# ---------------------------------------------------------------------------
if [ "$USE_GAR" = 1 ]; then
  log_step "GAR image pre-pull"
  TASKS_FILE="$(mktemp)"
  printf '%s\n' "${SELECTED[@]}" > "$TASKS_FILE"
  if python3 "${SCRIPT_DIR}/scripts/pull_tb_images.py" \
       --tasks-file "$TASKS_FILE" --concurrency "$CONCURRENCY"; then
    log_ok "GAR pre-pull complete"
  else
    log_warn "GAR pre-pull had failures — harbor will try Docker Hub for any missing image"
  fi
  rm -f "$TASKS_FILE"
fi

# ---------------------------------------------------------------------------
# Run id + agent dispatch (model formatting per agent, as in the prior run.sh)
# ---------------------------------------------------------------------------
# Harbor session logs: the eval-runner's log_sync only uploads three paths
# (eval-runner src/log_sync.rs sync_selective): runner_logs/, repo/output/,
# repo/logs/. run.sh's cwd is repo/, so ${SCRIPT_DIR}/logs == repo/logs is the
# synced location and what the artifacts zip captures.
SYNC_LOGS_DIR="${SCRIPT_DIR}/logs"
mkdir -p "$SYNC_LOGS_DIR"
TS="$(date +%Y-%m-%d__%H-%M-%S)"
JOB_ID="${EVAL_RUN_ID}__${AGENT//\//_}__${MODEL//\//_}__${TS}"

# ---------------------------------------------------------------------------
# DooD detection → harbor transfer mode.
# ---------------------------------------------------------------------------
# Harbor's docker environment normally bind-mounts each trial dir into the task
# container as /logs (that's how the agent transcript and verifier/reward.txt
# get out). Under DooD the *host* daemon resolves the bind-mount SOURCE on the
# HOST filesystem; the eval-runner's work dir is container-local, so the daemon
# mounts an empty orphan, reward.txt lands in the orphan, and every trial goes
# no-grade. swe-auto-eval never hits this because nothing in its stack relies
# on host paths — everything streams through the docker API.
#
# Harbor has the same DooD-safe mode built in: when an environment reports
# capabilities.mounted=False (as its cloud backends do), harbor fetches the
# verifier dir and agent logs with `docker compose cp` over the socket instead
# of reading bind-mounted paths. setup.sh installs a sitecustomize.py into the
# harbor venv that flips DockerEnvironment to that mode when
# TB_HARBOR_UNMOUNTED=1. Here we probe whether bind-mounts actually resolve
# (HOST-SHARED) and set the variable only when they don't, so native/manual
# VM runs keep today's mounted behavior bit-for-bit.
HARBOR_JOBS_DIR="$SYNC_LOGS_DIR"
RUN_DIR="$HARBOR_JOBS_DIR/$JOB_ID"

log_step "jobs-dir / DooD visibility"
echo "    DOCKER_HOST:     ${DOCKER_HOST:-<unset>}"
echo "    docker server:   $(timeout 10 docker version --format '{{.Server.Version}}' 2>/dev/null || echo '<unreachable>')"
echo "    harbor jobs-dir: $HARBOR_JOBS_DIR   (== repo/logs, uploaded to object storage by the eval-runner)"
echo "    run dir:         $RUN_DIR"

# Probe whether a daemon-launched container's bind-mounted write is visible to
# run.sh. Sets DOOD_ORPHAN: 1 = orphan (DooD, paths NOT shared), 0 = shared,
# "" = probe inconclusive. Non-fatal either way; the verdict drives the harbor
# transfer mode below and is logged unambiguously.
DOOD_ORPHAN=""
dood_shared_check() {
  local d="$1" img="" cand marker=".dood_probe_$$_${RANDOM}"
  command -v docker >/dev/null 2>&1 || { log_warn "[dood-check] docker CLI unavailable — skipping shared-path probe"; return; }
  # Use an image already present locally (GAR pre-pull leaves task images
  # cached) so the probe never depends on Docker Hub egress.
  for cand in $(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -v '<none>' | head -5); do
    if docker image inspect "$cand" >/dev/null 2>&1; then img="$cand"; break; fi
  done
  if [ -z "$img" ]; then
    log_warn "[dood-check] no local image available to probe with — verdict falls back to /.dockerenv heuristic"
    return
  fi
  mkdir -p "$d" 2>/dev/null
  if timeout 90 docker run --rm --entrypoint sh -v "$d:/probe" "$img" -c "echo shared > /probe/$marker" >/dev/null 2>&1; then
    if [ -f "$d/$marker" ]; then
      DOOD_ORPHAN=0
      log_ok "[dood-check] '$d' is HOST-SHARED → harbor bind-mounts resolve; keeping harbor's default mounted mode."
    else
      DOOD_ORPHAN=1
      log_warn "[dood-check] '$d' is NOT host-shared (DooD orphan): a daemon-launched container wrote into it but the file is invisible to run.sh."
    fi
    rm -f "$d/$marker" 2>/dev/null
  else
    log_warn "[dood-check] probe container failed to run (img=$img) — verdict falls back to /.dockerenv heuristic"
  fi
}
dood_shared_check "$HARBOR_JOBS_DIR"

# Inconclusive probe: if we're inside a container but talking to an outside
# daemon, that IS DooD — choose the safe mode. Plain VM: keep mounted mode.
if [ -z "$DOOD_ORPHAN" ]; then
  [ -f /.dockerenv ] && DOOD_ORPHAN=1 || DOOD_ORPHAN=0
  log_warn "[dood-check] probe inconclusive; /.dockerenv heuristic says DooD_orphan=$DOOD_ORPHAN"
fi

if [ "$DOOD_ORPHAN" = 1 ]; then
  export TB_HARBOR_UNMOUNTED=1
  log_ok "[dood-check] => TB_HARBOR_UNMOUNTED=1: harbor will fetch verifier/agent logs via 'docker compose cp' (socket-streamed, DooD-safe) instead of bind-mount reads."
else
  log_ok "[dood-check] => mounted mode (default): trial dirs are real host paths here."
fi

INCLUDE_FLAGS=()
for task in "${SELECTED[@]}"; do
  INCLUDE_FLAGS+=(--include-task-name "${DATASET_ORG}/${task}")
done

declare -A _MODEL_FMT=(
  [xyne-cli]="juspay/{MODEL}"
  [claude-code]="{MODEL}"
  [opencode]="Grid/{MODEL}"
)
if [[ "$MODEL" == */* ]]; then
  HARBOR_MODEL="$MODEL"
else
  FMT="${_MODEL_FMT[$AGENT]:-}"; [ -z "$FMT" ] && FMT="openai/{MODEL}"
  HARBOR_MODEL="${FMT//\{MODEL\}/$MODEL}"
fi

AGENT_FLAGS=()
if [ "$AGENT" = "xyne-cli" ]; then
  AGENT_FLAGS=(
    --agent-import-path xyne_harbor_agent.agent:XyneCliAgent
    --agent-env "XYNE_API_KEY=${XYNE_API_KEY}"
    --agent-env "XYNE_BASE_URL=${BASE_URL}"
  )
else
  AGENT_FLAGS=(--agent "$AGENT")
  case "$AGENT" in
    opencode)
      MODEL_ID="${HARBOR_MODEL#*/}"
      OC_CFG="{\"provider\":{\"Grid\":{\"npm\":\"@ai-sdk/openai-compatible\",\"name\":\"Grid AI\",\"options\":{\"baseURL\":\"${BASE_URL}\",\"apiKey\":\"${XYNE_API_KEY}\"},\"models\":{\"${MODEL_ID}\":{\"id\":\"${MODEL_ID}\",\"name\":\"${MODEL_ID}\",\"reasoning\":true,\"tool_call\":true}}}}}"
      AGENT_FLAGS+=(--ak "opencode_config=${OC_CFG}") ;;
    pi|aider)
      AGENT_FLAGS+=(--agent-env "OPENAI_BASE_URL=${BASE_URL}") ;;
  esac
fi

TIMEOUT_FLAGS=()
[ -n "$AGENT_TIMEOUT_MULT" ] && TIMEOUT_FLAGS+=(--agent-timeout-multiplier "$AGENT_TIMEOUT_MULT")

# ---------------------------------------------------------------------------
# Heartbeat: harbor's rich progress display goes silent when stdout is not a
# TTY, so on the dashboard the log is dark for the whole run. Poll the run dir
# and log STATE TRANSITIONS only — one line when a trial starts, one when it
# finishes (with its reward) — plus a one-line progress summary every 5 min.
# A trial is "finished" when harbor writes its result.json; the reward comes
# from verifier/reward.txt (present under both mounted and unmounted modes by
# the time result.json lands). Volume stays readable at any scale: 2 lines per
# trial + ~12 summary lines/hour, never a refresh flood.
# ---------------------------------------------------------------------------
EXPECTED_TRIALS=$((N_SEL * ATTEMPTS))

# ---------------------------------------------------------------------------
# harbor run (failure is captured, NOT fatal — we still write results).
# ---------------------------------------------------------------------------
log_step "harbor run (job: $JOB_ID)"
heartbeat & HB_PID=$!
HARBOR_RC=0
harbor run \
  "${AGENT_FLAGS[@]}" \
  --model "${HARBOR_MODEL}" \
  --dataset "$DATASET" \
  "${INCLUDE_FLAGS[@]}" \
  --jobs-dir "$HARBOR_JOBS_DIR" \
  --job-name "$JOB_ID" \
  --n-concurrent "$CONCURRENCY" \
  --n-attempts "$ATTEMPTS" \
  --quiet \
  "${TIMEOUT_FLAGS[@]}" \
  "${EXTRA_HARBOR_FLAGS[@]}" \
  --yes || HARBOR_RC=$?
stop_heartbeat
[ "$HARBOR_RC" -eq 0 ] && log_ok "harbor run finished" \
  || log_warn "harbor run exited $HARBOR_RC — aggregating whatever graded"

# ---------------------------------------------------------------------------
# Per-trial artifact visibility. Distinguishes the two failure modes in the
# run.sh log: a genuine agent/verifier failure (dirs present, reward missing or
# reward<1) vs. the DooD orphan bug (whole verifier/ + agent/ dirs missing).
# ---------------------------------------------------------------------------
log_step "trial artifacts ($RUN_DIR)"
if [ -d "$RUN_DIR" ]; then
  shopt -s nullglob
  _seen=0
  for _t in "$RUN_DIR"/*/; do
    _seen=1
    _name="$(basename "$_t")"
    if [ -f "${_t}verifier/reward.txt" ]; then
      _r="reward=$(tr -d '\n' < "${_t}verifier/reward.txt" 2>/dev/null)"
    else
      _r="reward.txt:MISSING"
    fi
    [ -d "${_t}verifier" ] && _v="verifier/:yes" || _v="verifier/:MISSING"
    [ -d "${_t}agent" ]    && _a="agent/:yes"    || _a="agent/:MISSING"
    echo "    ${_name}  ->  ${_r} | ${_v} | ${_a}"
  done
  [ "$_seen" = 0 ] && log_warn "    no trial directories under $RUN_DIR (harbor produced no trials)"
  shopt -u nullglob
else
  log_warn "    run dir does not exist: $RUN_DIR (harbor produced no job tree)"
fi

# ---------------------------------------------------------------------------
# Aggregate per-task (a task is SOLVED if reward>=1 in >=1 attempt) and emit
# the standardized results JSON the eval-runner reads. Same metric shape as
# swe-auto-eval's generate_results_json (main/secondary/additional). Reads
# RUN_DIR — harbor's authoritative output, where reward.txt actually lands.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Token usage & cost. Reads harbor's per-trial result.json (agent_result) plus
# verifier/reward.txt and writes token_usage.{json,csv,md} into OUTPUT_DIR —
# and .html when prices were supplied. Runs BEFORE the results aggregator so
# that aggregator can fold a headline into metrics.additional in one write.
#
# Strictly non-fatal: an accounting failure must never cost us a graded run.
# ---------------------------------------------------------------------------
log_step "token usage"
TOKEN_USAGE_JSON="${OUTPUT_DIR}/token_usage.json"
TOKEN_PRICE_FLAGS=()
[ -n "$PRICE_INPUT" ]  && TOKEN_PRICE_FLAGS+=(--price-input  "$PRICE_INPUT")
[ -n "$PRICE_OUTPUT" ] && TOKEN_PRICE_FLAGS+=(--price-output "$PRICE_OUTPUT")
[ -n "$PRICE_CACHED" ] && TOKEN_PRICE_FLAGS+=(--price-cached "$PRICE_CACHED")
[ -n "$PRICE_CACHE_WRITE" ] && TOKEN_PRICE_FLAGS+=(--price-cache-write "$PRICE_CACHE_WRITE")
if [ ${#TOKEN_PRICE_FLAGS[@]} -eq 0 ]; then
  log_info "no --price-* supplied; token counts will be reported unpriced"
fi
python3 "${SCRIPT_DIR}/analysis/token_usage.py" \
  --run-dir "$RUN_DIR" \
  --out-dir "$OUTPUT_DIR" \
  --eval-run-id "$EVAL_RUN_ID" \
  --agent "$AGENT" \
  --model "$MODEL" \
  --attempts "$ATTEMPTS" \
  "${TOKEN_PRICE_FLAGS[@]}" \
  || log_warn "token-usage reporting failed (non-fatal) — the run still counts"

log_step "results"

python3 "${SCRIPT_DIR}/analysis/results.py" \
  --run-dir "$RUN_DIR" \
  --token-usage "$TOKEN_USAGE_JSON" \
  --output "$RESULTS_FILE" \
  --agent "$AGENT" \
  --model "$MODEL" \
  --dataset "$DATASET" \
  --attempts "$ATTEMPTS" \
  --selected-tasks "$N_SEL" \
  --harbor-exit-code "$HARBOR_RC"
RESULTS_RC=$?

# Clear the always-write trap only if the real results file now exists.
if [ -f "$RESULTS_FILE" ] && [ "$RESULTS_RC" -eq 0 ]; then
  trap - EXIT
  log_ok "Standardized results written: $RESULTS_FILE"
else
  log_err "Results aggregation failed (rc=$RESULTS_RC) — fallback will be written"
  write_fallback_results "aggregation-failed rc=$RESULTS_RC"
  trap - EXIT
fi

# ---------------------------------------------------------------------------
# Human-readable scoreboard (operator convenience; not read by the harness).
# ---------------------------------------------------------------------------
echo
echo "=== scoreboard (solved = reward>=1 in >=1 of $ATTEMPTS attempts) ==="
python3 -c "
import json
m=json.load(open('$RESULTS_FILE'))['metrics']
s=m['secondary']
print('  solved      :', s.get('solved'))
print('  unsolved    :', s.get('unsolved'))
print('  no_grade    :', s.get('no_grade'))
print('  total       :', s.get('total'))
print('  solve_rate% :', s.get('solve_rate_pct'))
print('  trial acc%  :', s.get('trial_accuracy_pct'),
      '(' + str(s.get('successful_trials')) + '/' + str(s.get('planned_trials')) + ')')
" 2>/dev/null || true
echo
echo "Session logs (synced by the eval-runner as repo/logs/): $RUN_DIR/"
echo "  <trial>/verifier/reward.txt       — score"
echo "  <trial>/verifier/test-stdout.txt  — test output"
echo "  <trial>/agent/                    — agent logs"
[ "${TB_HARBOR_UNMOUNTED:-}" = 1 ] && echo "  (DooD: harbor fetched these via 'docker compose cp' — TB_HARBOR_UNMOUNTED=1)"
echo "Results JSON (synced as repo/output/, parsed by the runner): $RESULTS_FILE"
echo "Token usage (synced as repo/output/): ${OUTPUT_DIR}/token_usage.{json,csv,md}"
[ -f "${OUTPUT_DIR}/token_usage.html" ] && \
  echo "  priced report: ${OUTPUT_DIR}/token_usage.html"

# Exit 0 even if some tasks failed: the run COMPLETED and produced metrics.
# Only a missing results file (handled above) is a real failure for the harness.
exit 0
