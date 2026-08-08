# Pi Grid Harness Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Terminal-Bench's `pi` agent call Grid through an explicit Pi provider, safely accept every task prompt, surface agent failures, and reject infrastructure-invalid zero-activity runs without changing genuine benchmark scoring.

**Architecture:** Replace Harbor's generic built-in Pi dispatch with a repository-owned `PiGridAgent`. The adapter installs the latest Pi release from the current package namespace, writes Pi's supported custom-provider configuration, uploads the instruction as a file and pipes it over stdin, validates the final transcript, and populates Harbor usage fields. A separate post-run analyzer reports trial health and returns nonzero only when no trial contains genuine model activity; token parsing independently stops treating error-only zero usage as measured.

**Tech Stack:** Python 3.10+, Harbor 0.13.1 installed-agent API, Bash, `unittest`, Pi `@earendil-works/pi-coding-agent@latest`.

## Global Constraints

- Change only `terminal-bench-v2-agentic`.
- Do not modify eval-runner or dashboard code.
- Do not run Git commands.
- Preserve scoring: a genuine zero-reward run with real model activity remains completed.
- Preserve artifacts and standardized metrics even when the systemic-health gate fails.
- Never write the Grid key to agent logs or command-line arguments.

---

### Task 1: Transcript contract and token semantics

**Files:**
- Create: `adapter/pi_harbor_agent/transcript.py`
- Test: `adapter/tests/test_pi_transcript.py`
- Modify: `analysis/token_sources.py`
- Test: `analysis/test_token_sources.py`

**Interfaces:**
- Produces: `analyze_transcript(path: Path) -> TranscriptSummary` and `TranscriptError`.
- A transcript is active only when its final assistant `message_end` is not `error`/`aborted` and contains content or positive usage.

- [x] Write tests proving a 401 error-only transcript is rejected and not measured.
- [x] Run the focused tests and confirm they fail because the transcript module and zero-error filtering do not exist.
- [x] Implement JSONL analysis and change `_parse_pi` to ignore error-only zero-usage events while retaining real usage before a later error.
- [x] Run the focused tests and confirm they pass.

### Task 2: Grid-aware Pi Harbor adapter

**Files:**
- Create: `adapter/pi_harbor_agent/__init__.py`
- Create: `adapter/pi_harbor_agent/agent.py`
- Modify: `adapter/pyproject.toml`
- Test: `adapter/tests/test_pi_runtime.py`
- Test: `adapter/tests/test_pi_transcript.py`

**Interfaces:**
- Produces: `PiGridAgent`, selected by `pi_harbor_agent.agent:PiGridAgent`.
- Consumes: `PI_GRID_API_KEY`, `PI_GRID_BASE_URL`, and Harbor's parsed model name.

- [x] Write tests for a secret-safe `models.json` payload, latest-package selection, stdin/file prompt delivery, and post-run transcript failure propagation.
- [x] Run the adapter tests and confirm they fail because the Pi runtime/transcript modules do not exist.
- [x] Implement installation, provider configuration, prompt upload/stdin execution, transcript validation, and Harbor token population.
- [x] Run the adapter tests and confirm they pass.

### Task 3: Systemic run-health analyzer

**Files:**
- Create: `analysis/pi_health.py`
- Create: `analysis/test_pi_health.py`

**Interfaces:**
- Produces: `analyze_run(run_dir: Path) -> dict` and CLI output `pi_health.json`.
- CLI exits 0 when at least one Pi trial shows genuine model activity, 2 when trials exist but none show activity, and 3 when no trials exist.

- [x] Write tests for all-401, all-CLI-error, genuine zero-reward with model activity, and mixed healthy/error runs.
- [x] Run the tests and confirm they fail because the analyzer does not exist.
- [x] Implement the analyzer using transcript summaries plus `result.json.exception_info`.
- [x] Run the tests and confirm they pass.

### Task 4: Harness wiring and artifact-preserving failure

**Files:**
- Modify: `run.sh`
- Modify: `config.yaml`
- Modify: `analysis/results.py`
- Modify: `analysis/test_input_params_contract.py`
- Modify: `analysis/test_results.py`

**Interfaces:**
- Pi is dispatched as custom agent `pi_harbor_agent.agent:PiGridAgent` with model `juspay/<model>`.
- `results.py` accepts optional `--agent-health` and embeds the report in `metrics.additional.agent_health`.
- `run.sh` generates token/results artifacts before exiting with the health-gate status.

- [x] Write contract tests for Pi dispatch/model format and health metadata propagation.
- [x] Run focused tests and confirm they fail against the built-in OpenAI Pi path.
- [x] Wire the custom adapter, execute `pi_health.py` after Harbor, pass its report into results, and defer nonzero exit until artifact generation finishes.
- [x] Run focused tests and confirm they pass.

### Task 5: Full verification

**Files:**
- Verify all modified files.

- [x] Run all Terminal-Bench Python tests.
- [x] Run `bash -n run.sh setup.sh`.
- [x] Run `python3 -m py_compile` over adapter and analysis Python files.
- [x] Run the health analyzer against `artifacts_pi_terminal` and confirm it reports 0 active attempts and exits 2.
- [x] Recheck the implementation against every approved remedy and report any live smoke-test limitation explicitly.
