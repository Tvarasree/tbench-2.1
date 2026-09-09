"""Token accounting and engine detection for NATIVE xyne-cli session logs.

Deliberately free of any harbor import so it can be exercised without the
harness installed. `agent.py` maps the totals onto harbor's AgentContext.

Why this is not `xyne_harbor_agent.session_usage`
-------------------------------------------------
The native (Cordis plugin-kernel) engine and the embedded-Pi engine write
DIFFERENT formats into the SAME directory
(`~/.xyne/agent/sessions/<encoded-cwd>/*.jsonl`). Reusing the embedded reader
here would silently report zero tokens for every native run.

Native, one JSON object per line, header first:

    {"type":"xyne-native-session","version":1,"id":"<sid>","timestamp":"<ISO>","cwd":"<cwd>"}
    {"type":"xyne-native-session-entry","kind":"llm_usage","sessionId":"<sid>",
     "data":{"usage":{"inputTokens":1200,"outputTokens":340,
                      "cacheReadTokens":8000,"cacheWriteTokens":150},
             "finishReason":"stop","model":{...},"requestHeaderEntryId":"..."},
     "at":"<ISO>"}

Embedded-Pi writes `{"type":"session",...}` headers and carries usage on
assistant messages as `input`/`output`/`cacheRead`/`cacheWrite`. The foreign
native header is intentional (see `core-sessions/index.ts`): it exists so pi
tooling skips native files instead of mixing formats. That makes the header
line an exact, per-run engine fingerprint — which is what `classify_session_file`
below reads, and what lets a run prove it actually used the native engine
rather than merely asserting the env var was set.

Counter semantics match the canonical native reader (`sumNativeUsage` in
xyne-cli `src/agent/runtime/kernel-backed-runtime.ts`): the four buckets are
disjoint, `llm_usage` ledger rows are the source of truth, and the legacy
`assistant_message.data.usage` shape is a fallback used ONLY when a log
carries no ledger rows at all — never both, so a mixed log cannot double count.

Unlike the embedded format, native `llm_usage` rows carry no cost field; cost
is read opportunistically and is normally absent, leaving the reporter's
`--price-*` inputs to supply the money figures.
"""
from __future__ import annotations

import json
from pathlib import Path

# Disjoint token counters on a native usage block.
_TOKEN_FIELDS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")

# First-line `type` values that identify which engine wrote a session file.
NATIVE_HEADER_TYPE = "xyne-native-session"
EMBEDDED_HEADER_TYPE = "session"

ENGINE_NATIVE = "native-plugin-kernel"
ENGINE_EMBEDDED = "embedded-pi"
ENGINE_UNKNOWN = "unknown"


def _as_number(value: object) -> float | None:
    """Numeric fields only. bool is an int subclass; a stray true isn't 1."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _first_record(path: Path) -> dict | None:
    """Parse the first non-empty line of a JSONL file, or None."""
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return None
    with handle as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                return None
            return record if isinstance(record, dict) else None
    return None


def classify_session_file(path: Path) -> str:
    """Which engine wrote this session file, from its header line alone."""
    header = _first_record(path)
    if header is None:
        return ENGINE_UNKNOWN
    header_type = header.get("type")
    if header_type == NATIVE_HEADER_TYPE:
        return ENGINE_NATIVE
    if header_type == EMBEDDED_HEADER_TYPE:
        return ENGINE_EMBEDDED
    return ENGINE_UNKNOWN


def scan_engines(sessions_dir: Path) -> dict:
    """Count session files per engine below `sessions_dir`.

    The verdict is what the run actually did, not what it was asked to do:

      * `native`   — at least one native file and no embedded file.
      * `embedded` — at least one embedded file. The native flag did NOT take;
                     the trial measured the wrong engine.
      * `mixed`    — both present (e.g. a leaked probe session).
      * `none`     — nothing to judge from (no session files at all).
    """
    counts = {ENGINE_NATIVE: 0, ENGINE_EMBEDDED: 0, ENGINE_UNKNOWN: 0}
    files: list[str] = []
    if sessions_dir.is_dir():
        for path in sorted(sessions_dir.rglob("*.jsonl")):
            counts[classify_session_file(path)] += 1
            files.append(path.name)

    if counts[ENGINE_NATIVE] and counts[ENGINE_EMBEDDED]:
        verdict = "mixed"
    elif counts[ENGINE_NATIVE]:
        verdict = "native"
    elif counts[ENGINE_EMBEDDED]:
        verdict = "embedded"
    else:
        verdict = "none"

    return {
        "verdict": verdict,
        "native_files": counts[ENGINE_NATIVE],
        "embedded_files": counts[ENGINE_EMBEDDED],
        "unknown_files": counts[ENGINE_UNKNOWN],
        "files": files,
    }


def _usage_from_entry(record: dict, kind: str) -> dict | None:
    """The `data.usage` block of a native entry of the given kind, if present."""
    if record.get("kind") != kind:
        return None
    data = record.get("data")
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    return usage if isinstance(usage, dict) else None


def _accumulate(totals: dict, usage: dict) -> None:
    """Fold one usage block into the running totals."""
    for field in _TOKEN_FIELDS:
        value = _as_number(usage.get(field))
        if value is not None:
            totals[field] += int(value)
    cost = usage.get("cost")
    if isinstance(cost, dict):
        value = _as_number(cost.get("total"))
        if value is not None:
            totals["cost"] += value


def sum_session_usage(sessions_dir: Path) -> dict | None:
    """Sum the native `llm_usage` ledger over every native session JSONL below a dir.

    Returns None when the directory is absent or no native session carried
    usage, so the caller can leave harbor's context empty (treated as
    unmeasured) instead of reporting a misleading zero.

    Embedded-Pi session files sharing the directory are skipped by header, so
    a mixed directory never mixes the two engines' counters.
    """
    if not sessions_dir.is_dir():
        return None

    totals = {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadTokens": 0,
        "cacheWriteTokens": 0,
        "cost": 0.0,
        "files": 0,
        "ledger_rows": 0,
        "fallback_messages": 0,
    }
    # Ledger rows win outright; assistant-message usage is only consulted when
    # the whole scan found no ledger row, so the two are never added together.
    fallback: list[dict] = []

    for path in sorted(sessions_dir.rglob("*.jsonl")):
        if classify_session_file(path) != ENGINE_NATIVE:
            continue
        totals["files"] += 1
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    # A run killed on timeout leaves a truncated final line.
                    continue
                if not isinstance(record, dict):
                    continue

                usage = _usage_from_entry(record, "llm_usage")
                if usage is not None:
                    totals["ledger_rows"] += 1
                    _accumulate(totals, usage)
                    continue

                legacy = _usage_from_entry(record, "assistant_message")
                if legacy is not None:
                    fallback.append(legacy)

    if totals["ledger_rows"] == 0:
        for usage in fallback:
            totals["fallback_messages"] += 1
            _accumulate(totals, usage)

    measured = totals["ledger_rows"] or totals["fallback_messages"]
    return totals if measured else None


def to_harbor_fields(totals: dict) -> dict:
    """Map the native counters onto harbor's AgentContext field names.

    harbor documents `n_input_tokens` as input *including* cache, so the cache
    counters are folded into it as well as reported separately.
    """
    cache_read = totals["cacheReadTokens"]
    cache_write = totals["cacheWriteTokens"]
    return {
        "n_input_tokens": totals["inputTokens"] + cache_read + cache_write,
        # Harbor's cache field is cache reads. Cache writes remain part of
        # input and are retained separately by the raw-session reporter.
        "n_cache_tokens": cache_read,
        "n_output_tokens": totals["outputTokens"],
        # grid.ai self-hosted models are priced at 0 upstream, and the native
        # ledger carries no cost field at all, so an all-zero cost means "not
        # priced", not "free" — leave it unset and let the reporter's
        # --price-* inputs supply the money figures.
        "cost_usd": totals["cost"] or None,
    }
