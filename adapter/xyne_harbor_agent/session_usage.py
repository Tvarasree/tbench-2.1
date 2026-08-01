"""Token accounting for xyne-cli session transcripts.

Deliberately free of any harbor import so it can be exercised without the
harness installed. `agent.py` maps the totals onto harbor's AgentContext.

xyne writes one JSON object per line into
`<agent-dir>/sessions/<encoded-cwd>/<ts>_<id>.jsonl`. The first line is a
SessionHeader; the rest are messages. Only assistant messages carry `usage`:

    {"role": "assistant",
     "usage": {"input": 1200, "output": 340, "cacheRead": 8000,
               "cacheWrite": 150, "totalTokens": 9690,
               "cost": {"total": 0.0}}}

`input`, `cacheRead` and `cacheWrite` are disjoint counters — xyne sums them
independently when it computes its own totals (`buildStateSnapshot` in
xyne-cli src/agent/serve.ts), and we sum the same way so the two agree.

Known limit: there is no terminal summary record, so subagent turns are only
counted if pi persisted them into this session file. Reconcile against a
finished run before trusting absolute numbers.
"""
from __future__ import annotations

import json
from pathlib import Path

# Disjoint token counters on a usage block.
_TOKEN_FIELDS = ("input", "output", "cacheRead", "cacheWrite")


def _as_number(value: object) -> float | None:
    """Numeric fields only. bool is an int subclass; a stray true isn't 1."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def sum_session_usage(sessions_dir: Path) -> dict | None:
    """Sum `usage` over assistant messages in every session JSONL below a dir.

    Returns None when the directory is absent or no assistant message carried a
    usage block, so the caller can leave harbor's context empty (treated as
    unmeasured) instead of reporting a misleading zero.
    """
    if not sessions_dir.is_dir():
        return None

    totals = {
        "input": 0,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "cost": 0.0,
        "files": 0,
        "messages": 0,
    }

    for path in sorted(sessions_dir.rglob("*.jsonl")):
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
                # Skips the leading SessionHeader and every non-assistant
                # entry; only assistant messages carry usage.
                if record.get("role") != "assistant":
                    continue
                usage = record.get("usage")
                if not isinstance(usage, dict):
                    continue
                totals["messages"] += 1
                for field in _TOKEN_FIELDS:
                    value = _as_number(usage.get(field))
                    if value is not None:
                        totals[field] += int(value)
                cost = usage.get("cost")
                if isinstance(cost, dict):
                    value = _as_number(cost.get("total"))
                    if value is not None:
                        totals["cost"] += value

    return totals if totals["messages"] else None


def to_harbor_fields(totals: dict) -> dict:
    """Map xyne's counters onto harbor's AgentContext field names.

    harbor documents `n_input_tokens` as input *including* cache, so the cache
    counters are folded into it as well as reported separately.
    """
    cache = totals["cacheRead"] + totals["cacheWrite"]
    return {
        "n_input_tokens": totals["input"] + cache,
        "n_cache_tokens": cache,
        "n_output_tokens": totals["output"],
        # xyne prices self-hosted grid.ai models at 0, so an all-zero cost means
        # "not priced upstream", not "free" — leave it unset and let the
        # reporter's --price-* inputs supply the money figures.
        "cost_usd": totals["cost"] or None,
    }
