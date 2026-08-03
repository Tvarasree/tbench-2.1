#!/usr/bin/env python3
"""
Token usage & cost reporting for a completed terminal-bench (harbor) run.

Reads only what the run already wrote to disk — no API calls, no extra
instrumentation.

Sources
    {run_dir}/{trial}/agent/
        Agent-native artifacts are preferred because Harbor 0.13.1 drops
        cache-write fields for some agents and has no Aider token adapter.
        Supported sources cover xyne-cli, claude-code, opencode, pi, aider,
        goose, and codex.

    {run_dir}/{trial}/result.json
        Harbor fallback. `agent_result` (an AgentContext) carries per-trial
        totals: n_input_tokens (input INCLUDING cache), n_cache_tokens,
        n_output_tokens, cost_usd. Multi-step trials leave
        `agent_result` null and record one context per entry of `step_results`;
        both shapes are aggregated, mirroring harbor's own
        TrialResult.compute_token_cost_totals().

    {run_dir}/{trial}/verifier/reward.txt
        The trial's score. reward >= 1 is a solve. A missing file is a
        no-grade, which is also how run.sh classifies it.

Coverage is a first-class output. Unmeasured trials are disproportionately the
killed/timed-out ones — i.e. the expensive ones — so any total taken with
coverage < 100% is a LOWER BOUND and is labelled as such. Measurements also
carry `full`, `partial`, `total_only`, or `unmeasured` quality; only a full
input/output split is eligible for custom pricing.

Outputs (written to --out-dir)
    token_usage.json    per-attempt records + summary + coverage
    token_usage.csv     flat, one row per attempt
    token_usage.md      readable tables
    token_usage.html    priced visual report — only when prices are supplied

Pricing is optional and expressed in USD per 1,000,000 tokens. Supplying both
--price-input and --price-output unlocks the priced report; supplying only one
leaves the run unpriced with a stated reason rather than half-pricing it.
--price-cached and --price-cache-write are optional on top. Their respective
tokens use the supplied rate, or the input rate when omitted. Without input
and output prices, token reports are still emitted and money fields are null.

Usage:
    python3 token_usage.py --run-dir DIR --out-dir DIR [--eval-run-id ID]
        [--agent A] [--model M] [--attempts N]
        [--price-input F] [--price-output F] [--price-cached F]
        [--price-cache-write F]

Exit code is always 0: a reporting failure must never fail an otherwise
successful eval run.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import pathlib
import sys
from datetime import datetime, timezone
from typing import Any

_ADAPTER_DIR = pathlib.Path(__file__).resolve().parents[1] / "adapter"
if str(_ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_DIR))

from xyne_harbor_agent.session_usage import sum_session_usage  # noqa: E402

try:  # Supports both `python analysis/token_usage.py` and package imports.
    from .token_sources import parse_agent_usage
except ImportError:  # pragma: no cover - exercised by standalone invocation
    from token_sources import parse_agent_usage  # type: ignore[no-redef]

MILLION = 1_000_000

# Outcome buckets, in report order. Mirrors run.sh's aggregator exactly so the
# two files can never disagree about what "solved" means.
OUTCOMES = ("solved", "unsolved", "no-grade")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _read_json(path: pathlib.Path) -> Any:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _contexts_from_result(result: Any) -> list[dict]:
    """Return the AgentContext dicts on a TrialResult, whichever shape it used.

    Single-step trials set `agent_result`; multi-step trials leave it null and
    put one context on each entry of `step_results`. Same precedence harbor
    itself applies in TrialResult.compute_token_cost_totals().
    """
    if not isinstance(result, dict):
        return []
    agent_result = result.get("agent_result")
    if isinstance(agent_result, dict):
        return [agent_result]
    steps = result.get("step_results")
    if isinstance(steps, list):
        return [
            s["agent_result"]
            for s in steps
            if isinstance(s, dict) and isinstance(s.get("agent_result"), dict)
        ]
    return []


def _sum_contexts(
    contexts: list[dict],
) -> tuple[int, int, int, float | None, bool, set[str]]:
    """Return input, cache, output, billed cost, measurement flag, and fields.

    `measured` is False when no context carried a single token field — an agent
    harbor does not instrument, or a trial that died before reporting. A context
    of all-nulls is not evidence of zero usage.
    """
    n_input = n_cache = n_output = 0
    cost = 0.0
    saw_tokens = False
    saw_cost = False
    observed_fields: set[str] = set()
    for ctx in contexts:
        for key in ("n_input_tokens", "n_cache_tokens", "n_output_tokens"):
            value = ctx.get(key)
            if isinstance(value, (int, float)):
                saw_tokens = True
                observed_fields.add(key)
                if key == "n_input_tokens":
                    n_input += int(value)
                elif key == "n_cache_tokens":
                    n_cache += int(value)
                else:
                    n_output += int(value)
        value = ctx.get("cost_usd")
        if isinstance(value, (int, float)):
            saw_cost = True
            cost += float(value)
    return (
        n_input,
        n_cache,
        n_output,
        (cost if saw_cost else None),
        saw_tokens,
        observed_fields,
    )


def load_attempts(run_dir: pathlib.Path, agent: str = "") -> list[dict]:
    """One record per trial directory. A trial is one attempt at one task."""
    attempts: list[dict] = []
    if not run_dir.is_dir():
        return attempts

    for trial_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        name = trial_dir.name
        # harbor names trial dirs "<task>__<suffix>"; run.sh splits the same way.
        task = name.split("__")[0] if "__" in name else name

        reward: float | None = None
        reward_file = trial_dir / "verifier" / "reward.txt"
        if reward_file.is_file():
            try:
                reward = float(reward_file.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                reward = None

        if reward is None:
            outcome = "no-grade"
        elif reward >= 1:
            outcome = "solved"
        else:
            outcome = "unsolved"

        result = _read_json(trial_dir / "result.json")
        contexts = _contexts_from_result(result)
        n_input, n_cache, n_output, cost, measured, observed_fields = _sum_contexts(
            contexts
        )

        # harbor fills AgentInfo.version from the agent's get_version_command()
        # (`xyne --version` for xyne-cli). setup.sh installs @xyne/xyne-cli
        # UNPINNED, so this is the only per-run record of which build actually
        # ran — capture it rather than relying on a setup.sh log line that a
        # cache-hit would skip entirely.
        agent_version = ""
        agent_info = result.get("agent_info") if isinstance(result, dict) else None
        if isinstance(agent_info, dict):
            agent_version = str(agent_info.get("version") or "")
        result_agent = str(agent_info.get("name") or "") if isinstance(agent_info, dict) else ""

        usage_source = "harbor-agent-context" if measured else "unmeasured"
        measurement_quality = (
            "full"
            if {"n_input_tokens", "n_output_tokens"}.issubset(observed_fields)
            else "partial" if measured else "unmeasured"
        )
        priceable = measurement_quality == "full"
        total_override: int | None = None
        n_cache_read = n_cache
        n_cache_write = 0
        selected_agent = agent or result_agent
        if selected_agent == "goose" and observed_fields == {"n_input_tokens"}:
            # Harbor 0.13.1 puts Goose's combined total in n_input_tokens.
            # Preserve the count without lying about its input/output split.
            total_override = n_input
            n_input = n_cache = n_output = n_cache_read = 0
            measurement_quality = "total_only"
            priceable = False
        # Xyne's raw transcript retains separate cache-read/cache-write fields
        # that Harbor AgentContext cannot represent. Prefer it even when the
        # adapter populated the lossy Harbor totals; it is also the recovery
        # path when Harbor copied the logs after populate_context_post_run().
        if selected_agent == "xyne-cli":
            totals = sum_session_usage(trial_dir / "agent" / "sessions")
            if totals is not None:
                n_cache_read = totals["cacheRead"]
                n_cache_write = totals["cacheWrite"]
                n_cache = n_cache_read + n_cache_write
                n_input = totals["input"] + n_cache
                n_output = totals["output"]
                cost = totals["cost"] or None
                measured = True
                usage_source = "xyne-session-jsonl"
                measurement_quality = "full"
                priceable = True
                total_override = None
        else:
            raw_usage = parse_agent_usage(trial_dir, selected_agent)
            if raw_usage is not None:
                n_input = raw_usage.n_input_tokens
                n_cache_read = raw_usage.n_cache_read_tokens
                n_cache_write = raw_usage.n_cache_write_tokens
                n_cache = n_cache_read + n_cache_write
                n_output = raw_usage.n_output_tokens
                cost = raw_usage.cost_usd
                measured = True
                usage_source = raw_usage.source
                measurement_quality = raw_usage.quality
                priceable = raw_usage.quality == "full"
                total_override = None

        attempts.append(
            {
                "trial": name,
                "task": task,
                "outcome": outcome,
                "reward": reward,
                "agent_version": agent_version,
                "measured": measured,
                "usage_source": usage_source,
                "measurement_quality": measurement_quality,
                "priceable": priceable,
                "n_input_tokens": n_input,
                "n_cache_tokens": n_cache,
                "n_cache_read_tokens": n_cache_read,
                "n_cache_write_tokens": n_cache_write,
                "n_output_tokens": n_output,
                "n_total_tokens": (
                    total_override if total_override is not None else n_input + n_output
                ),
                "cost_usd_billed": cost,
            }
        )

    # Attempt index is positional within a task: harbor's trial suffix is not a
    # documented ordinal, so we number by sorted trial name and say so.
    per_task_seen: dict[str, int] = {}
    for record in sorted(attempts, key=lambda r: (r["task"], r["trial"])):
        per_task_seen[record["task"]] = per_task_seen.get(record["task"], 0) + 1
        record["attempt_index"] = per_task_seen[record["task"]]
    return attempts


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
class Pricing:
    """USD per 1M tokens. Either fully priced or not priced at all."""

    def __init__(
        self,
        price_input: float | None,
        price_output: float | None,
        price_cached: float | None,
        price_cache_write: float | None = None,
    ) -> None:
        self.price_input = price_input
        self.price_output = price_output
        self.price_cached = price_cached
        self.price_cache_write = price_cache_write
        self.enabled = bool(price_input) and bool(price_output)

        if self.enabled:
            if price_cached or price_cache_write:
                read_rate = price_cached or price_input
                write_rate = price_cache_write or price_input
                self.note = (
                    f"Priced at ${price_input:g} input / ${price_output:g} output / "
                    f"${read_rate:g} cache read / ${write_rate:g} cache write per "
                    f"1M tokens. Unspecified cache rates use the input rate."
                )
            else:
                self.note = (
                    f"Priced at ${price_input:g} input / ${price_output:g} output per "
                    f"1M tokens. No cached rate supplied, so ALL input tokens "
                    f"(including cache reads) are billed at the input rate — this "
                    f"overstates cost when caching is active."
                )
        elif price_input or price_output or price_cached or price_cache_write:
            self.note = (
                "NOT PRICED: both --price-input and --price-output are required. "
                "A partial price list is never half-applied, because a half-priced "
                "total is worse than no total."
            )
        else:
            self.note = (
                "NOT PRICED: no --price-input/--price-output supplied. Token counts "
                "available from completed artifacts are still reported; money "
                "figures are absent."
            )

    def cost(
        self,
        n_input: int,
        n_cache_read: int,
        n_output: int,
        n_cache_write: int = 0,
    ) -> float | None:
        if not self.enabled:
            return None
        cache_read_rate = self.price_cached if self.price_cached else self.price_input
        cache_write_rate = (
            self.price_cache_write if self.price_cache_write else self.price_input
        )
        # n_input already includes cache (harbor's documented semantics), so the
        # uncached remainder is the difference. Clamp: a malformed context could
        # report more cache than input.
        uncached = max(n_input - n_cache_read - n_cache_write, 0)
        return (
            uncached * self.price_input
            + n_cache_read * cache_read_rate
            + n_cache_write * cache_write_rate
            + n_output * self.price_output
        ) / MILLION


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def _blank_bucket() -> dict:
    return {
        "attempts": 0,
        "measured_attempts": 0,
        "priceable_attempts": 0,
        "n_input_tokens": 0,
        "n_cache_tokens": 0,
        "n_cache_read_tokens": 0,
        "n_cache_write_tokens": 0,
        "n_output_tokens": 0,
        "n_total_tokens": 0,
        "cost_usd_billed": 0.0,
        "cost_usd_priced": 0.0,
    }


def _add(bucket: dict, record: dict) -> None:
    bucket["attempts"] += 1
    if record["measured"]:
        bucket["measured_attempts"] += 1
    if record["priceable"]:
        bucket["priceable_attempts"] += 1
    for key in (
        "n_input_tokens",
        "n_cache_tokens",
        "n_cache_read_tokens",
        "n_cache_write_tokens",
        "n_output_tokens",
        "n_total_tokens",
    ):
        bucket[key] += record[key]
    if record["cost_usd_billed"]:
        bucket["cost_usd_billed"] += record["cost_usd_billed"]
    if record["cost_usd_priced"]:
        bucket["cost_usd_priced"] += record["cost_usd_priced"]


def _finish(bucket: dict) -> dict:
    n = bucket["attempts"]
    bucket["avg_total_tokens_per_attempt"] = (
        round(bucket["n_total_tokens"] / n, 1) if n else 0.0
    )
    bucket["avg_cost_usd_per_attempt"] = (
        round(bucket["cost_usd_priced"] / n, 6) if n else 0.0
    )
    bucket["cost_usd_billed"] = round(bucket["cost_usd_billed"], 6)
    bucket["cost_usd_priced"] = round(bucket["cost_usd_priced"], 6)
    return bucket


def aggregate(attempts: list[dict], pricing: Pricing) -> dict:
    for record in attempts:
        record["cost_usd_priced"] = (
            pricing.cost(
                record["n_input_tokens"],
                record["n_cache_read_tokens"],
                record["n_output_tokens"],
                record["n_cache_write_tokens"],
            )
            if record["priceable"]
            else None
        )

    total = _blank_bucket()
    by_outcome = {outcome: _blank_bucket() for outcome in OUTCOMES}
    by_round: dict[int, dict] = {}
    by_task: dict[str, dict] = {}

    for record in attempts:
        _add(total, record)
        _add(by_outcome[record["outcome"]], record)

        rnd = record["attempt_index"]
        bucket = by_round.setdefault(rnd, _blank_bucket())
        bucket["solves"] = bucket.get("solves", 0) + (record["outcome"] == "solved")
        _add(bucket, record)

        task = by_task.setdefault(
            record["task"],
            {
                "attempts": 0,
                "solved": False,
                "measured_attempts": 0,
                "priceable_attempts": 0,
                "n_total_tokens_all_attempts": 0,
                "cost_usd_all_attempts": 0.0,
                "n_total_tokens_winning_attempt": 0,
                "cost_usd_winning_attempt": 0.0,
            },
        )
        task["attempts"] += 1
        task["measured_attempts"] += int(record["measured"])
        task["priceable_attempts"] += int(record["priceable"])
        task["n_total_tokens_all_attempts"] += record["n_total_tokens"]
        task["cost_usd_all_attempts"] += record["cost_usd_priced"] or 0.0
        # The winning attempt is the FIRST solve; later attempts of an
        # already-solved task are not what bought the solve.
        if record["outcome"] == "solved" and not task["solved"]:
            task["solved"] = True
            task["n_total_tokens_winning_attempt"] = record["n_total_tokens"]
            task["cost_usd_winning_attempt"] = record["cost_usd_priced"] or 0.0

    solved_tasks = [t for t in by_task.values() if t["solved"]]
    n_solved = len(solved_tasks)

    # Cost that actually bought a solve, vs everything else. The honest
    # cost-per-success is the one that carries the failed retries.
    cost_winning = sum(t["cost_usd_winning_attempt"] for t in solved_tasks)
    tokens_winning = sum(t["n_total_tokens_winning_attempt"] for t in solved_tasks)
    cost_total = total["cost_usd_priced"]
    tokens_total = total["n_total_tokens"]

    for bucket in list(by_outcome.values()) + list(by_round.values()):
        _finish(bucket)
    for bucket in by_round.values():
        solves = bucket.get("solves", 0)
        bucket["cost_usd_per_solve"] = (
            round(bucket["cost_usd_priced"] / solves, 6)
            if solves and bucket["priceable_attempts"]
            else None
        )
    for task in by_task.values():
        task["cost_usd_all_attempts"] = round(task["cost_usd_all_attempts"], 6)
        task["cost_usd_winning_attempt"] = round(task["cost_usd_winning_attempt"], 6)
        if task["priceable_attempts"] == 0:
            task["cost_usd_all_attempts"] = None
            task["cost_usd_winning_attempt"] = None

    n_attempts = total["attempts"]
    n_measured = total["measured_attempts"]
    n_priceable = sum(int(record["priceable"]) for record in attempts)
    quality_counts = {
        quality: sum(
            int(record["measurement_quality"] == quality) for record in attempts
        )
        for quality in ("full", "partial", "total_only", "unmeasured")
    }
    _finish(total)

    for bucket in [total, *by_outcome.values(), *by_round.values()]:
        if not pricing.enabled or bucket["priceable_attempts"] == 0:
            bucket["cost_usd_priced"] = None
            bucket["avg_cost_usd_per_attempt"] = None
            if "cost_usd_per_solve" in bucket:
                bucket["cost_usd_per_solve"] = None

    if not pricing.enabled:
        for task in by_task.values():
            task["cost_usd_all_attempts"] = None
            task["cost_usd_winning_attempt"] = None

    if not any(record["cost_usd_billed"] is not None for record in attempts):
        for bucket in [total, *by_outcome.values(), *by_round.values()]:
            bucket["cost_usd_billed"] = None

    return {
        "coverage": {
            "measured_attempts": n_measured,
            "priceable_attempts": n_priceable,
            "total_attempts": n_attempts,
            "quality_counts": quality_counts,
            "measured_pct": (
                round(100.0 * n_measured / n_attempts, 2) if n_attempts else 0.0
            ),
            "complete": n_measured == n_attempts and n_attempts > 0,
            "note": (
                "Totals are a LOWER BOUND: unmeasured attempts contribute 0 and are "
                "disproportionately the killed/timed-out (expensive) ones."
                if n_measured != n_attempts
                else "All attempts reported token usage."
            ),
        },
        "totals": total,
        "by_outcome": by_outcome,
        "by_round": {str(k): v for k, v in sorted(by_round.items())},
        "by_task": dict(sorted(by_task.items())),
        "cost_per_success": {
            "tasks_solved": n_solved,
            # Lead with this one: it is what a solve actually costs.
            "cost_usd_per_solve_including_failed_retries": (
                round(cost_total / n_solved, 6)
                if pricing.enabled and n_priceable and n_solved else None
            ),
            "cost_usd_per_solve_winning_attempt_only": (
                round(cost_winning / n_solved, 6)
                if pricing.enabled and n_priceable and n_solved else None
            ),
            "tokens_per_solve_including_failed_retries": (
                round(tokens_total / n_solved, 1) if n_solved else None
            ),
            "tokens_per_solve_winning_attempt_only": (
                round(tokens_winning / n_solved, 1) if n_solved else None
            ),
        },
        "waste": {
            "cost_usd_attributable_to_a_solve": (
                round(cost_winning, 6) if pricing.enabled and n_priceable else None
            ),
            "cost_usd_wasted": (
                round(cost_total - cost_winning, 6)
                if pricing.enabled and n_priceable else None
            ),
            "wasted_pct": (
                round(100.0 * (cost_total - cost_winning) / cost_total, 2)
                if pricing.enabled and n_priceable and cost_total
                else None
            ),
            "tokens_attributable_to_a_solve": tokens_winning,
            "tokens_wasted": tokens_total - tokens_winning,
        },
    }


# ---------------------------------------------------------------------------
# Emitters
# ---------------------------------------------------------------------------
def _fmt_usd(value: float | None) -> str:
    return "—" if value is None else f"${value:,.4f}"


def _fmt_int(value: Any) -> str:
    return f"{value:,}" if isinstance(value, (int, float)) else "—"


def write_csv(path: pathlib.Path, attempts: list[dict]) -> None:
    columns = [
        "task",
        "attempt_index",
        "trial",
        "outcome",
        "reward",
        "agent_version",
        "measured",
        "usage_source",
        "measurement_quality",
        "priceable",
        "n_input_tokens",
        "n_cache_tokens",
        "n_cache_read_tokens",
        "n_cache_write_tokens",
        "n_output_tokens",
        "n_total_tokens",
        "cost_usd_billed",
        "cost_usd_priced",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for record in sorted(attempts, key=lambda r: (r["task"], r["attempt_index"])):
            writer.writerow(record)


def write_markdown(path: pathlib.Path, report: dict) -> None:
    meta = report["meta"]
    agg = report["aggregates"]
    cov = agg["coverage"]
    totals = agg["totals"]
    lines: list[str] = []
    add = lines.append

    add(f"# Token usage — {meta['eval_run_id']}")
    add("")
    versions = meta.get("agent_versions") or []
    version_text = ", ".join(versions) if versions else "unrecorded"
    add(f"- agent: `{meta['agent']}` (version: {version_text})  model: `{meta['model']}`")
    if len(versions) > 1:
        add(
            f"- ⚠️ **{len(versions)} distinct agent versions in one run** — the "
            f"binary changed mid-flight, so these tasks are not comparable."
        )
    add(f"- attempts per task: {meta['attempts']}")
    add(f"- generated: {meta['generated_at']}")
    add("")
    add(f"> **Pricing.** {meta['pricing_note']}")
    add("")
    add(
        f"> **Coverage.** {cov['measured_attempts']}/{cov['total_attempts']} attempts "
        f"measured ({cov['measured_pct']}%); {cov['priceable_attempts']} have a full "
        f"priceable split. {cov['note']}"
    )
    add("")

    add("## Totals")
    add("")
    add("| Metric | Value |")
    add("|---|---|")
    add(f"| Input tokens (incl. cache) | {_fmt_int(totals['n_input_tokens'])} |")
    add(f"| — cache read | {_fmt_int(totals['n_cache_read_tokens'])} |")
    add(f"| — cache write | {_fmt_int(totals['n_cache_write_tokens'])} |")
    add(f"| Output tokens | {_fmt_int(totals['n_output_tokens'])} |")
    add(f"| Total tokens | {_fmt_int(totals['n_total_tokens'])} |")
    add(f"| Cost (as billed upstream) | {_fmt_usd(totals['cost_usd_billed'] or None)} |")
    add(f"| Cost (at supplied prices) | {_fmt_usd(totals['cost_usd_priced'] or None)} |")
    add("")
    if totals["n_cache_tokens"] == 0 and totals["n_input_tokens"] > 0:
        add(
            "> **No prompt caching detected** — every input token was billed at the "
            "full input rate. In agentic loops the input:output ratio runs ~100:1, so "
            "the input rate sets the bill; enabling caching is usually a bigger cost "
            "lever than changing model."
        )
        add("")

    add("## By outcome")
    add("")
    add(
        "| Outcome | Attempts | Measured | Total tokens | Avg tokens/attempt | "
        "Cost | Avg cost/attempt |"
    )
    add("|---|---:|---:|---:|---:|---:|---:|")
    for outcome in OUTCOMES:
        b = agg["by_outcome"][outcome]
        add(
            f"| {outcome} | {b['attempts']} | {b['measured_attempts']} | "
            f"{_fmt_int(b['n_total_tokens'])} | {b['avg_total_tokens_per_attempt']:,.1f} | "
            f"{_fmt_usd(b['cost_usd_priced'] or None)} | "
            f"{_fmt_usd(b['avg_cost_usd_per_attempt'] or None)} |"
        )
    add("")
    add(
        "Compare the two *averages*, not the totals: a failing attempt that costs as "
        "much as a succeeding one is the finding that changes decisions about retry "
        "budgets."
    )
    add("")

    cps = agg["cost_per_success"]
    add("## Cost per success")
    add("")
    add(f"- tasks solved: **{cps['tasks_solved']}**")
    add(
        f"- cost per solve, **including its failed retries**: "
        f"**{_fmt_usd(cps['cost_usd_per_solve_including_failed_retries'])}** "
        f"({_fmt_int(cps['tokens_per_solve_including_failed_retries'])} tokens)"
    )
    add(
        f"- cost per solve, winning attempt only: "
        f"{_fmt_usd(cps['cost_usd_per_solve_winning_attempt_only'])} "
        f"({_fmt_int(cps['tokens_per_solve_winning_attempt_only'])} tokens)"
    )
    add("")
    waste = agg["waste"]
    add(
        f"- spend that bought a solve: "
        f"{_fmt_usd(waste['cost_usd_attributable_to_a_solve'] or None)}; "
        f"**wasted: {_fmt_usd(waste['cost_usd_wasted'] or None)}**"
        + (f" ({waste['wasted_pct']}%)" if waste["wasted_pct"] is not None else "")
    )
    add("")

    add("## By attempt round")
    add("")
    add("| Round | Attempts | Solves | Total tokens | Cost | Cost per solve |")
    add("|---:|---:|---:|---:|---:|---:|")
    for rnd, b in agg["by_round"].items():
        add(
            f"| {rnd} | {b['attempts']} | {b.get('solves', 0)} | "
            f"{_fmt_int(b['n_total_tokens'])} | {_fmt_usd(b['cost_usd_priced'] or None)} | "
            f"{_fmt_usd(b.get('cost_usd_per_solve'))} |"
        )
    add("")
    add("Where cost-per-solve climbs sharply is where retry economics collapse.")
    add("")

    add("## Per task")
    add("")
    add(
        "| Task | Attempts | Solved | Measured | Tokens (all attempts) | "
        "Cost (all attempts) |"
    )
    add("|---|---:|:--:|---:|---:|---:|")
    for name, t in agg["by_task"].items():
        add(
            f"| {name} | {t['attempts']} | {'yes' if t['solved'] else 'no'} | "
            f"{t['measured_attempts']}/{t['attempts']} | "
            f"{_fmt_int(t['n_total_tokens_all_attempts'])} | "
            f"{_fmt_usd(t['cost_usd_all_attempts'] or None)} |"
        )
    add("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_html(path: pathlib.Path, report: dict) -> None:
    """Priced visual report. Only called when pricing is enabled."""
    meta = report["meta"]
    agg = report["aggregates"]
    totals = agg["totals"]
    cov = agg["coverage"]
    cps = agg["cost_per_success"]
    waste = agg["waste"]

    def esc(value: Any) -> str:
        return (
            str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )

    def usd(value: float | None) -> str:
        return "&mdash;" if value is None else f"${value:,.4f}"

    outcome_rows = "".join(
        "<tr><td>{o}</td><td>{a}</td><td>{tk:,}</td><td>{av:,.1f}</td>"
        "<td>{c}</td><td>{ac}</td></tr>".format(
            o=esc(outcome),
            a=agg["by_outcome"][outcome]["attempts"],
            tk=agg["by_outcome"][outcome]["n_total_tokens"],
            av=agg["by_outcome"][outcome]["avg_total_tokens_per_attempt"],
            c=usd(agg["by_outcome"][outcome]["cost_usd_priced"]),
            ac=usd(agg["by_outcome"][outcome]["avg_cost_usd_per_attempt"]),
        )
        for outcome in OUTCOMES
    )
    round_rows = "".join(
        "<tr><td>{r}</td><td>{a}</td><td>{s}</td><td>{tk:,}</td>"
        "<td>{c}</td><td>{cps}</td></tr>".format(
            r=esc(rnd),
            a=b["attempts"],
            s=b.get("solves", 0),
            tk=b["n_total_tokens"],
            c=usd(b["cost_usd_priced"]),
            cps=(
                usd(b["cost_usd_per_solve"])
                if b.get("cost_usd_per_solve") is not None
                else "&mdash;"
            ),
        )
        for rnd, b in agg["by_round"].items()
    )
    task_rows = "".join(
        "<tr><td>{n}</td><td>{a}</td><td>{s}</td><td>{tk:,}</td>"
        "<td>{c}</td></tr>".format(
            n=esc(name),
            a=t["attempts"],
            s="yes" if t["solved"] else "no",
            tk=t["n_total_tokens_all_attempts"],
            c=usd(t["cost_usd_all_attempts"]),
        )
        for name, t in agg["by_task"].items()
    )

    coverage_banner = (
        ""
        if cov["complete"]
        else f'<p class="warn"><strong>Lower bound.</strong> {esc(cov["note"])}</p>'
    )

    path.write_text(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Token usage — {esc(meta['eval_run_id'])}</title>
<style>
 :root {{ color-scheme: light dark; --fg:#1a1a1a; --bg:#fff; --mut:#666;
          --line:#e3e3e3; --accent:#0b5fff; --warnbg:#fff6e5; --warnfg:#8a5a00; }}
 @media (prefers-color-scheme: dark) {{
   :root {{ --fg:#e8e8e8; --bg:#141416; --mut:#a0a0a0; --line:#2e2e33;
            --accent:#6fa2ff; --warnbg:#3a2f14; --warnfg:#ffd88a; }} }}
 body {{ font: 15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
         color:var(--fg); background:var(--bg); margin:0; padding:2rem 1.25rem; }}
 main {{ max-width: 60rem; margin: 0 auto; }}
 h1 {{ font-size:1.5rem; margin:0 0 .25rem; }}
 h2 {{ font-size:1.1rem; margin:2rem 0 .5rem; border-bottom:1px solid var(--line);
       padding-bottom:.3rem; }}
 .sub {{ color:var(--mut); margin:0 0 1.25rem; font-size:.9rem; }}
 .cards {{ display:grid; gap:.75rem;
           grid-template-columns:repeat(auto-fit,minmax(11rem,1fr)); margin:1rem 0; }}
 .card {{ border:1px solid var(--line); border-radius:.5rem; padding:.75rem .9rem; }}
 .card .k {{ color:var(--mut); font-size:.78rem; text-transform:uppercase;
             letter-spacing:.04em; }}
 .card .v {{ font-size:1.35rem; font-weight:600; margin-top:.2rem; }}
 .warn {{ background:var(--warnbg); color:var(--warnfg); padding:.7rem .9rem;
          border-radius:.5rem; font-size:.9rem; }}
 .note {{ color:var(--mut); font-size:.87rem; }}
 .scroll {{ overflow-x:auto; }}
 table {{ border-collapse:collapse; width:100%; font-size:.9rem; }}
 th,td {{ text-align:right; padding:.4rem .6rem; border-bottom:1px solid var(--line);
          white-space:nowrap; }}
 th:first-child, td:first-child {{ text-align:left; }}
 th {{ color:var(--mut); font-weight:600; }}
 strong.big {{ color:var(--accent); }}
</style></head><body><main>
<h1>Token usage &mdash; {esc(meta['eval_run_id'])}</h1>
<p class="sub">agent <code>{esc(meta['agent'])}</code> &middot;
 model <code>{esc(meta['model'])}</code> &middot;
 {esc(meta['attempts'])} attempts/task &middot; {esc(meta['generated_at'])}</p>
<p class="note">{esc(meta['pricing_note'])}</p>
{coverage_banner}
<div class="cards">
 <div class="card"><div class="k">Total tokens</div>
   <div class="v">{totals['n_total_tokens']:,}</div></div>
 <div class="card"><div class="k">Cost (priced)</div>
   <div class="v">{usd(totals['cost_usd_priced'])}</div></div>
 <div class="card"><div class="k">Tasks solved</div>
   <div class="v">{cps['tasks_solved']}</div></div>
 <div class="card"><div class="k">Coverage</div>
   <div class="v">{cov['measured_pct']}%</div></div>
</div>
<h2>Cost per success</h2>
<p>Including failed retries: <strong class="big">
 {usd(cps['cost_usd_per_solve_including_failed_retries'])}</strong>
 per solve &mdash; the honest figure. Winning attempt only:
 {usd(cps['cost_usd_per_solve_winning_attempt_only'])}.</p>
<p>Spend that bought a solve: {usd(waste['cost_usd_attributable_to_a_solve'])}.
 <strong>Wasted: {usd(waste['cost_usd_wasted'])}</strong>
 ({waste['wasted_pct'] if waste['wasted_pct'] is not None else 0}%).</p>
<h2>By outcome</h2>
<div class="scroll"><table>
<tr><th>Outcome</th><th>Attempts</th><th>Tokens</th><th>Avg tokens/attempt</th>
    <th>Cost</th><th>Avg cost/attempt</th></tr>
{outcome_rows}
</table></div>
<p class="note">Compare the averages, not the totals: failing attempts that cost
 as much as succeeding ones is what changes retry-budget decisions.</p>
<h2>By attempt round</h2>
<div class="scroll"><table>
<tr><th>Round</th><th>Attempts</th><th>Solves</th><th>Tokens</th><th>Cost</th>
    <th>Cost per solve</th></tr>
{round_rows}
</table></div>
<h2>Per task</h2>
<div class="scroll"><table>
<tr><th>Task</th><th>Attempts</th><th>Solved</th><th>Tokens (all attempts)</th>
    <th>Cost (all attempts)</th></tr>
{task_rows}
</table></div>
</main></body></html>
""",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def build_report(attempts: list[dict], pricing: Pricing, meta: dict) -> dict:
    aggregates = aggregate(attempts, pricing)
    return {
        "meta": meta,
        "aggregates": aggregates,
        "attempts": sorted(attempts, key=lambda r: (r["task"], r["attempt_index"])),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Token usage & cost report for a harbor terminal-bench run."
    )
    parser.add_argument(
        "--run-dir", required=True, help="harbor job dir holding trial dirs"
    )
    parser.add_argument("--out-dir", required=True, help="where the reports are written")
    parser.add_argument("--eval-run-id", default="")
    parser.add_argument("--agent", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--attempts", default="")
    # Units are USD per 1M tokens. Saying so here and in every label is
    # deliberate: entered per-token, these land the report 10^6 off.
    parser.add_argument(
        "--price-input", type=float, default=None,
        help="USD per 1,000,000 input tokens",
    )
    parser.add_argument(
        "--price-output", type=float, default=None,
        help="USD per 1,000,000 output tokens",
    )
    parser.add_argument(
        "--price-cached", type=float, default=None,
        help="USD per 1,000,000 cached-read tokens (optional)",
    )
    parser.add_argument(
        "--price-cache-write", type=float, default=None,
        help="USD per 1,000,000 cache-write tokens (optional)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir = pathlib.Path(args.run_dir)
    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pricing = Pricing(
        args.price_input,
        args.price_output,
        args.price_cached,
        args.price_cache_write,
    )
    attempts = load_attempts(run_dir, agent=args.agent)

    if not attempts:
        print(f"[token-usage] no trial dirs under {run_dir} — nothing to report")
        return 0

    meta = {
        "eval_run_id": args.eval_run_id or run_dir.name,
        "agent": args.agent,
        "model": args.model,
        "attempts": args.attempts,
        "run_dir": str(run_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "priced": pricing.enabled,
        "price_input_per_1m_usd": args.price_input,
        "price_output_per_1m_usd": args.price_output,
        "price_cached_per_1m_usd": args.price_cached,
        "price_cache_write_per_1m_usd": args.price_cache_write,
        "pricing_note": pricing.note,
        "usage_source": (
            "agent-native raw artifacts when available, otherwise Harbor "
            "TrialResult.agent_result; outcomes from verifier/reward.txt"
        ),
    }

    # More than one version across a run means the agent binary changed
    # mid-flight (unpinned always-latest install + a release landing between
    # VM provisioning steps). That invalidates cross-task comparison, so it is
    # surfaced, not averaged over.
    versions = sorted({a["agent_version"] for a in attempts if a["agent_version"]})
    meta["agent_versions"] = versions
    if len(versions) > 1:
        print(
            f"[token-usage] WARNING: {len(versions)} distinct agent versions in "
            f"one run ({', '.join(versions)}) — results are not comparable."
        )

    report = build_report(attempts, pricing, meta)

    json_path = out_dir / "token_usage.json"
    tmp = json_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, indent=2), encoding="utf-8")
    os.replace(tmp, json_path)

    write_csv(out_dir / "token_usage.csv", attempts)
    write_markdown(out_dir / "token_usage.md", report)
    if pricing.enabled:
        write_html(out_dir / "token_usage.html", report)

    cov = report["aggregates"]["coverage"]
    totals = report["aggregates"]["totals"]
    print(
        f"[token-usage] → {out_dir}/token_usage.{{json,csv,md}}"
        + (",html" if pricing.enabled else "")
    )
    print(
        f"[token-usage] coverage {cov['measured_attempts']}/{cov['total_attempts']} "
        f"({cov['measured_pct']}%) | total tokens {totals['n_total_tokens']:,}"
        + (
            f" | cost ${totals['cost_usd_priced']:,.4f}"
            if pricing.enabled and totals["cost_usd_priced"] is not None
            else " | token split unavailable for pricing"
            if pricing.enabled
            else " | unpriced"
        )
    )
    if not cov["complete"]:
        print(f"[token-usage] WARNING: {cov['note']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — reporting must never fail a run
        print(f"[token-usage] reporting failed (non-fatal): {exc!r}", file=sys.stderr)
        sys.exit(0)
