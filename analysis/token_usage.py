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
    token_usage.html    visual report; cost-first when priced, token-first otherwise

Pricing is optional and expressed in USD per 1,000,000 tokens. Supplying both
--price-input and --price-output unlocks cost views in the visual report;
supplying only one leaves the run token-only with a stated reason rather than
half-pricing it.
Every measured input token uses the input rate and every measured output token
uses the output rate. Without both prices, token reports are still emitted and
money fields are null.

Usage:
    python3 token_usage.py --run-dir DIR --out-dir DIR [--eval-run-id ID]
        [--agent A] [--model M] [--attempts N]
        [--price-input F] [--price-output F]

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

    for trial_dir in sorted(
        p for p in run_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    ):
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
            elif selected_agent == "pi" and (trial_dir / "agent" / "pi.txt").is_file():
                # Harbor may serialize a synthetic all-zero AgentContext even
                # when Pi only emitted an API/CLI error. A present transcript
                # with no parseable usage is unmeasured, not measured zero.
                n_input = n_cache = n_cache_read = n_cache_write = n_output = 0
                cost = None
                measured = False
                usage_source = "pi-transcript-unmeasured"
                measurement_quality = "unmeasured"
                priceable = False
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
    ) -> None:
        self.price_input = price_input
        self.price_output = price_output
        self.enabled = bool(price_input) and bool(price_output)

        if self.enabled:
            self.note = (
                f"Priced at ${price_input:g} input / ${price_output:g} output per "
                f"1M tokens."
            )
        elif price_input or price_output:
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
        n_output: int,
    ) -> float | None:
        if not self.enabled:
            return None
        return (
            n_input * self.price_input
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
                record["n_output_tokens"],
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

    successful_records = [
        record
        for record in attempts
        if record["reward"] is not None and record["reward"] > 0
    ]
    measured_successful_records = [
        record for record in successful_records if record["measured"]
    ]
    priceable_successful_records = [
        record for record in successful_records if record["priceable"]
    ]
    tokens_successful = sum(
        record["n_total_tokens"] for record in measured_successful_records
    )
    cost_successful = sum(
        record["cost_usd_priced"] or 0.0
        for record in priceable_successful_records
    )

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
        bucket["tokens_per_solve"] = (
            round(bucket["n_total_tokens"] / solves, 1) if solves else None
        )
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
            "successful_trials": len(successful_records),
            "measured_successful_trials": len(measured_successful_records),
            "tokens_successful_trials": tokens_successful,
            "avg_tokens_per_successful_trial": (
                round(tokens_successful / len(measured_successful_records), 1)
                if measured_successful_records
                else None
            ),
            "priceable_successful_trials": len(priceable_successful_records),
            "cost_usd_successful_trials": (
                round(cost_successful, 6)
                if pricing.enabled and priceable_successful_records
                else None
            ),
            "avg_cost_usd_per_successful_trial": (
                round(cost_successful / len(priceable_successful_records), 6)
                if pricing.enabled and priceable_successful_records
                else None
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
            "tokens_wasted_pct": (
                round(100.0 * (tokens_total - tokens_winning) / tokens_total, 2)
                if tokens_total
                else None
            ),
        },
    }


# ---------------------------------------------------------------------------
# Emitters
# ---------------------------------------------------------------------------
def _fmt_usd(value: float | None) -> str:
    return "—" if value is None else f"${value:,.4f}"


def _fmt_int(value: Any) -> str:
    return f"{value:,}" if isinstance(value, (int, float)) else "—"


def _fmt_html_tokens(value: Any) -> str:
    """Compact token values for HTML only; machine-readable reports stay exact."""
    if not isinstance(value, (int, float)):
        return "—" if value is None else str(value)
    if abs(value) < 1_000:
        if isinstance(value, float) and value.is_integer():
            return f"{int(value):,}"
        return _fmt_int(value)

    divisor, suffix = (
        (1_000_000, "M") if abs(value) >= 1_000_000 else (1_000, "K")
    )
    scaled = round(value / divisor, 2)
    if suffix == "K" and abs(scaled) >= 1_000:
        scaled, suffix = round(value / 1_000_000, 2), "M"
    return f"{scaled:.2f}".rstrip("0").rstrip(".") + suffix


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
    add(
        f"- **Average successful trial**: "
        f"{_fmt_int(cps['avg_tokens_per_successful_trial'])} tokens; "
        f"{_fmt_usd(cps['avg_cost_usd_per_successful_trial'])} "
        f"({cps['measured_successful_trials']}/{cps['successful_trials']} "
        f"successful trials measured, {cps['priceable_successful_trials']} priceable)"
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
    """Write a self-contained cost-first or token-first visual report."""
    meta = report["meta"]
    agg = report["aggregates"]
    totals = agg["totals"]
    cov = agg["coverage"]
    cps = agg["cost_per_success"]
    waste = agg["waste"]
    priced = bool(meta.get("priced"))
    has_billed = totals["cost_usd_billed"] is not None
    show_billed_column = priced or has_billed

    def esc(value: Any) -> str:
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#x27;")
        )

    def usd(value: float | None) -> str:
        return "&mdash;" if value is None else f"${value:,.4f}"

    def number(value: int | float | None) -> str:
        if value is None:
            return "&mdash;"
        if isinstance(value, float) and not value.is_integer():
            return f"{value:,.1f}"
        return f"{int(value):,}"

    def tokens(value: int | float | None) -> str:
        return "&mdash;" if value is None else f"{_fmt_html_tokens(value)} tokens"

    def percentage(value: int | float | None) -> str:
        return "&mdash;" if value is None else f"{value:,.2f}"

    def width(value: int | float | None, peak: int | float | None) -> float:
        if value is None or peak is None or peak <= 0:
            return 0.0
        return min(100.0, 100.0 * value / peak)

    labels = {"solved": "Solved", "unsolved": "Unsolved", "no-grade": "No grade"}
    colours = {
        "solved": "var(--blue)",
        "unsolved": "var(--orange)",
        "no-grade": "var(--green)",
    }

    stack_segments: list[str] = []
    stack_legend: list[str] = []
    outcome_rows: list[str] = []
    outcome_bars: list[str] = []
    total_tokens = totals["n_total_tokens"]
    outcome_peak = max(
        (agg["by_outcome"][outcome]["n_total_tokens"] for outcome in OUTCOMES),
        default=0,
    )
    for outcome in OUTCOMES:
        bucket = agg["by_outcome"][outcome]
        label = labels[outcome]
        colour = colours[outcome]
        share = width(bucket["n_total_tokens"], total_tokens)
        inline = (
            f'<span class="in-label">{label} {share:.1f}%</span>'
            if share >= 20
            else ""
        )
        stack_segments.append(
            f'<span style="width:{share:.2f}%;background:{colour}">{inline}</span>'
        )
        stack_legend.append(
            f'<div><span class="swatch" style="background:{colour}"></span>'
            f'{label} &mdash; {share:.1f}%</div>'
        )
        outcome_bars.append(
            f'<div class="row"><div class="name">{label}</div>'
            f'<div class="track"><div class="fill" data-chart="outcome" style="width:'
            f'{width(bucket["n_total_tokens"], outcome_peak):.2f}%;background:{colour}">'
            f'</div></div><div class="val">{_fmt_html_tokens(bucket["n_total_tokens"])} tokens</div></div>'
        )
        outcome_rows.append(
            f'<tr><td><span class="key"><span class="swatch" style="background:{colour}">'
            f'</span>{label}</span></td><td class="n">{bucket["attempts"]}</td>'
            f'<td class="n">{bucket["measured_attempts"]}</td>'
            f'<td class="n">{bucket["priceable_attempts"]}</td>'
            f'<td class="n">{_fmt_html_tokens(bucket["n_input_tokens"])}</td>'
            f'<td class="n">{_fmt_html_tokens(bucket["n_output_tokens"])}</td>'
            f'<td class="n">{_fmt_html_tokens(bucket["n_total_tokens"])}</td>'
            f'<td class="n">{_fmt_html_tokens(bucket["avg_total_tokens_per_attempt"])}</td>'
            + (
                f'<td class="n">{usd(bucket["cost_usd_priced"])}</td>'
                f'<td class="n">{usd(bucket["avg_cost_usd_per_attempt"])}</td>'
                if priced
                else ""
            )
            + (
                f'<td class="n">{usd(bucket["cost_usd_billed"])}</td>'
                if show_billed_column
                else ""
            )
            + "</tr>"
        )

    success_scenarios = [
        (
            "Including failed retries",
            cps["tokens_per_solve_including_failed_retries"],
            cps["cost_usd_per_solve_including_failed_retries"],
            "All measured run usage divided by tasks solved at least once.",
        ),
        (
            "Winning attempt only",
            cps["tokens_per_solve_winning_attempt_only"],
            cps["cost_usd_per_solve_winning_attempt_only"],
            "Only the first successful attempt for each solved task.",
        ),
        (
            "Average successful trial",
            cps["avg_tokens_per_successful_trial"],
            cps["avg_cost_usd_per_successful_trial"],
            f'{cps["measured_successful_trials"]}/{cps["successful_trials"]} '
            + (
                f'successful trials measured; {cps["priceable_successful_trials"]} priceable.'
                if priced
                else "successful trials measured."
            ),
        ),
    ]
    success_peak = max(
        ((cost if priced else token_count) or 0 for _, token_count, cost, _ in success_scenarios),
        default=0,
    )
    success_cards = "".join(
        f'<div class="scenario"><div class="scenario-head"><div><div class="k">{label}</div>'
        f'<div class="scenario-value">{usd(cost) if priced else tokens(token_count)}</div></div>'
        + (
            f'<div class="token-value">{tokens(token_count)}</div>' if priced else ""
        )
        + "</div>"
        f'<div class="mini-track"><div class="fill" data-chart="success" '
        f'style="width:{width(cost if priced else token_count, success_peak):.2f}%">'
        f'</div></div><p>{esc(note)}</p></div>'
        for label, token_count, cost, note in success_scenarios
    )

    round_peak = max(
        (
            (
                bucket.get("cost_usd_per_solve")
                if priced
                else bucket.get("tokens_per_solve")
            )
            or 0
            for bucket in agg["by_round"].values()
        ),
        default=0,
    )
    round_bars: list[str] = []
    round_rows: list[str] = []
    for rnd, bucket in agg["by_round"].items():
        label = f"Attempt {esc(rnd)}"
        round_value = (
            bucket.get("cost_usd_per_solve")
            if priced
            else bucket.get("tokens_per_solve")
        )
        round_display = usd(round_value) if priced else tokens(round_value)
        if priced:
            round_display += (
                f'<span class="token-value">'
                f'{tokens(bucket.get("tokens_per_solve"))}</span>'
            )
        round_bars.append(
            f'<div class="row"><div class="name">{label} &mdash; '
            f'{bucket.get("solves", 0)} solves</div><div class="track">'
            f'<div class="fill" data-chart="round" '
            f'style="width:{width(round_value, round_peak):.2f}%;'
            f'background:var(--blue)"></div></div>'
            f'<div class="val">{round_display}</div></div>'
        )
        round_rows.append(
            f'<tr><td>{label}</td><td class="n">{bucket["attempts"]}</td>'
            f'<td class="n">{bucket.get("solves", 0)}</td>'
            f'<td class="n">{bucket["measured_attempts"]}</td>'
            f'<td class="n">{bucket["priceable_attempts"]}</td>'
            f'<td class="n">{_fmt_html_tokens(bucket["n_input_tokens"])}</td>'
            f'<td class="n">{_fmt_html_tokens(bucket["n_output_tokens"])}</td>'
            f'<td class="n">{_fmt_html_tokens(bucket["n_total_tokens"])}</td>'
            f'<td class="n">{_fmt_html_tokens(bucket["avg_total_tokens_per_attempt"])}</td>'
            + (
                f'<td class="n">{usd(bucket["cost_usd_priced"])}</td>'
                f'<td class="n">{usd(bucket["avg_cost_usd_per_attempt"])}</td>'
                if priced
                else ""
            )
            + (
                f'<td class="n">{usd(bucket["cost_usd_billed"])}</td>'
                if show_billed_column
                else ""
            )
            + f'<td class="n">{usd(round_value) if priced else tokens(round_value)}</td></tr>'
        )

    task_rows = "".join(
        f'<tr><td>{esc(name)}</td><td class="n">{task["attempts"]}</td>'
        f'<td>{"yes" if task["solved"] else "no"}</td>'
        f'<td class="n">{task["measured_attempts"]}/{task["attempts"]}</td>'
        f'<td class="n">{task["priceable_attempts"]}/{task["attempts"]}</td>'
        f'<td class="n">{_fmt_html_tokens(task["n_total_tokens_all_attempts"])}</td>'
        + (
            f'<td class="n">{usd(task["cost_usd_all_attempts"])}</td>'
            if priced
            else ""
        )
        + f'<td class="n">{_fmt_html_tokens(task["n_total_tokens_winning_attempt"])}</td>'
        + (
            f'<td class="n">{usd(task["cost_usd_winning_attempt"] if task["solved"] else None)}</td>'
            if priced
            else ""
        )
        + "</tr>"
        for name, task in agg["by_task"].items()
    )

    quality = cov["quality_counts"]
    quality_summary = " &middot; ".join(
        f'{esc(name.replace("_", " "))} {count}'
        for name, count in quality.items()
    )
    coverage_banner = (
        ""
        if cov["complete"]
        else f'<p class="warn"><strong>Lower bound.</strong> {esc(cov["note"])}</p>'
    )
    versions = meta.get("agent_versions") or []
    version_text = ", ".join(esc(version) for version in versions) or "not reported"

    if priced:
        report_title = "Terminal-Bench Token Usage &amp; Cost"
        mode_label = "Custom-priced report"
        hero_label = "Cost per solved task, including failed retries"
        hero_value = usd(cps["cost_usd_per_solve_including_failed_retries"])
        hero_secondary = (
            f'<div class="token">'
            f'{_fmt_html_tokens(cps["tokens_per_solve_including_failed_retries"])} '
            f'tokens per solved task</div>'
        )
        hero_foot = (
            f'{cps["tasks_solved"]} tasks solved &middot; {totals["attempts"]} trials '
            f'&middot; {usd(totals["cost_usd_priced"])} custom-priced total'
        )
        pricing_tile = (
            f'<div class="tile"><div class="k">Custom-priced cost</div>'
            f'<div class="v">{usd(totals["cost_usd_priced"])}</div>'
            f'<div class="foot">Billed upstream: {usd(totals["cost_usd_billed"])}</div></div>'
        )
        coverage_foot = (
            f'{cov["measured_attempts"]}/{cov["total_attempts"]} measured; '
            f'{cov["priceable_attempts"]} priceable'
        )
        successful_totals = (
            f'<p class="note">Successful-trial totals: '
            f'{_fmt_html_tokens(cps["tokens_successful_trials"])} measured tokens and\n'
            f'{usd(cps["cost_usd_successful_trials"])} across '
            f'{cps["successful_trials"]} successful trials.</p>'
        )
        outcome_sub = (
            "Share and volume of measured tokens. Exact token components and "
            "costs remain in the table."
        )
        outcome_cost_headers = (
            '<th class="n">Priced cost</th><th class="n">Avg cost/trial</th>'
        )
        outcome_billed_header = '<th class="n">Billed cost</th>'
        round_title = "Economics by attempt round"
        round_sub = (
            "Bars compare custom-priced cost per solve; the table retains every "
            "round-level token and cost metric."
        )
        round_cost_headers = (
            '<th class="n">Priced cost</th><th class="n">Avg cost/trial</th>'
        )
        round_billed_header = '<th class="n">Billed cost</th>'
        round_value_header = '<th class="n">Cost/solve</th>'
        waste_cards = (
            f'<div class="waste-item"><div class="k">Attributable to solves</div>'
            f'<div class="v">{usd(waste["cost_usd_attributable_to_a_solve"])}</div>\n'
            f'<div class="foot">{_fmt_html_tokens(waste["tokens_attributable_to_a_solve"])} tokens</div></div>\n'
            f'<div class="waste-item bad"><div class="k">Not attributable to solves</div>'
            f'<div class="v">{usd(waste["cost_usd_wasted"])}</div>\n'
            f'<div class="foot">{_fmt_html_tokens(waste["tokens_wasted"])} tokens &middot; '
            f'{number(waste["wasted_pct"])}% of custom-priced cost</div></div>'
        )
        task_headers = (
            '<th class="n">All tokens</th><th class="n">All cost</th>'
            '<th class="n">Winning tokens</th><th class="n">Winning cost</th>'
        )
        methodology = (
            "Custom prices are supplied with the run and are not provider-verified. "
            "Every measured input token uses the input rate and every measured "
            "output token uses the output rate."
        )
    else:
        report_title = "Terminal-Bench Token Usage"
        mode_label = "Token-only report"
        hero_label = "Tokens per solved task, including all trials"
        hero_value = (
            f'{tokens(cps["tokens_per_solve_including_failed_retries"])} per solved task'
            if cps["tokens_per_solve_including_failed_retries"] is not None
            else "&mdash;"
        )
        hero_secondary = ""
        hero_foot = (
            f'{cps["tasks_solved"]} tasks solved &middot; {totals["attempts"]} trials '
            f'&middot; {mode_label}'
        )
        pricing_tile = (
            f'<div class="tile"><div class="k">Upstream billed cost '
            f'(secondary telemetry)</div><div class="v">'
            f'{usd(totals["cost_usd_billed"])}</div>'
            f'<div class="foot">Provider-reported; not used in comparisons</div></div>'
            if has_billed
            else ""
        )
        coverage_foot = (
            f'{cov["measured_attempts"]}/{cov["total_attempts"]} measured; '
            f'{cov["priceable_attempts"]} with full input/output split'
        )
        successful_totals = (
            f'<p class="note">Successful-trial totals: '
            f'{_fmt_html_tokens(cps["tokens_successful_trials"])} measured tokens across '
            f'{cps["successful_trials"]} successful trials.</p>'
        )
        outcome_sub = (
            "Share and volume of measured tokens. Exact token components remain "
            "in the table."
        )
        outcome_cost_headers = ""
        outcome_billed_header = (
            '<th class="n">Upstream billed</th>' if has_billed else ""
        )
        round_title = "Efficiency by attempt round"
        round_sub = (
            "Bars compare measured tokens per successful trial in each attempt "
            "round; exact token totals remain in the table."
        )
        round_cost_headers = ""
        round_billed_header = (
            '<th class="n">Upstream billed</th>' if has_billed else ""
        )
        round_value_header = '<th class="n">Tokens/solve</th>'
        waste_cards = (
            f'<div class="waste-item"><div class="k">Attributable to solves</div>'
            f'<div class="v">{tokens(waste["tokens_attributable_to_a_solve"])}</div>\n'
            f'<div class="foot">First winning attempts only</div></div>\n'
            f'<div class="waste-item bad"><div class="k">Not attributable to solves</div>'
            f'<div class="v">{tokens(waste["tokens_wasted"])}</div>\n'
            f'<div class="foot">{percentage(waste["tokens_wasted_pct"])}% of measured tokens</div></div>'
        )
        task_headers = (
            '<th class="n">All tokens</th><th class="n">Winning tokens</th>'
        )
        methodology = (
            "Custom input and output prices were not both supplied, so monetary "
            "comparisons are omitted and measured tokens are the primary unit."
        )

    css = """
:root{color-scheme:light;--page:#f8f9fb;--surface:#fff;--text:#101114;
--secondary:#555b66;--muted:#858b96;--line:#e2e5ea;--baseline:#c7ccd4;
--border:rgba(16,17,20,.10);--blue:#2a78d6;--orange:#e96832;--green:#159b6c;
--amber:#d68a00;--track:#e9edf2;--warn-bg:#fff6df;--warn-text:#775100;
--shadow:0 12px 35px rgba(30,45,70,.06)}
@media(prefers-color-scheme:dark){:root{color-scheme:dark;--page:#0d0f12;
--surface:#181b20;--text:#f5f7fa;--secondary:#c2c7cf;--muted:#8e949e;
--line:#2b3038;--baseline:#3b424c;--border:rgba(255,255,255,.10);
--blue:#438ee8;--orange:#e36a39;--green:#22a879;--amber:#e4a11b;
--track:#252a31;--warn-bg:#352b14;--warn-text:#ffd98d;
--shadow:0 12px 35px rgba(0,0,0,.20)}}
*{box-sizing:border-box}body{margin:0;padding:2.5rem 1.25rem 5rem;
background:var(--page);color:var(--text);font:15px/1.6 system-ui,-apple-system,
"Segoe UI",sans-serif}.wrap{max-width:72rem;margin:0 auto}header{margin-bottom:2.25rem}
h1{font-size:1.8rem;line-height:1.2;margin:0 0 .55rem;letter-spacing:-.025em}
.meta,.sub,.note{color:var(--secondary);font-size:.875rem}.meta{margin:0}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.84em;
background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:.1em .35em}
section{margin-bottom:2.8rem}h2{font-size:1.08rem;margin:0 0 .35rem;letter-spacing:-.01em}
h2 .num{color:var(--muted);margin-right:.45rem}.sub{margin:0 0 1.1rem}
.card,.tile,.scenario{background:var(--surface);border:1px solid var(--border);
border-radius:12px;box-shadow:var(--shadow)}.card{padding:1.35rem 1.5rem}
.hero{margin-bottom:1rem;background:linear-gradient(135deg,var(--surface),color-mix(in srgb,var(--blue) 7%,var(--surface)))}
.hero .k,.tile .k,.scenario .k{color:var(--secondary);font-size:.78rem}
.hero .v{font-size:3rem;font-weight:650;line-height:1;letter-spacing:-.04em;margin:.25rem 0}
.hero .token{font-size:1rem;color:var(--blue);font-weight:600}.hero .foot{color:var(--secondary);font-size:.84rem;margin-top:.45rem}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(10rem,1fr));gap:.75rem}
.tile{padding:1rem 1.1rem}.tile .v{font-size:1.55rem;font-weight:650;line-height:1.2;margin-top:.2rem}
.tile .foot{color:var(--muted);font-size:.74rem;margin-top:.25rem}
.warn{background:var(--warn-bg);color:var(--warn-text);border:1px solid color-mix(in srgb,var(--amber) 35%,transparent);
padding:.75rem 1rem;border-radius:9px;font-size:.88rem}.success-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr));gap:.75rem}
.scenario{padding:1rem 1.1rem}.scenario-head{display:flex;justify-content:space-between;gap:1rem;align-items:flex-end}
.scenario-value{font-size:1.65rem;font-weight:650;letter-spacing:-.025em}.token-value{display:block;font-size:.82rem;color:var(--secondary);text-align:right}
.scenario p{color:var(--muted);font-size:.76rem;margin:.55rem 0 0}.mini-track,.track{background:var(--track);overflow:hidden}
.mini-track{height:5px;border-radius:99px;margin-top:.8rem}.mini-track .fill,.fill{height:100%;background:var(--blue)}
.stack{display:flex;gap:2px;height:26px;margin:.2rem 0 .85rem;overflow:hidden;border-radius:5px;background:var(--track)}
.stack>span{position:relative;min-width:0}.in-label{position:absolute;inset:0;display:flex;align-items:center;padding-left:.6rem;
font-size:.72rem;font-weight:650;color:#fff;white-space:nowrap}.legend{display:flex;flex-wrap:wrap;gap:.35rem 1.15rem;margin-bottom:1rem}
.legend div{display:flex;align-items:center;gap:.42rem;font-size:.8rem;color:var(--secondary)}
.swatch{width:10px;height:10px;border-radius:2px;display:inline-block;flex:none}.bars{display:grid;gap:.7rem;margin-bottom:1.15rem}
.row{display:grid;grid-template-columns:minmax(8rem,13rem) 1fr auto;gap:.85rem;align-items:center}
.row .name{font-size:.82rem;color:var(--secondary)}.track{height:18px;border-radius:0 4px 4px 0}
.row .val{font-size:.82rem;font-weight:600;min-width:7rem;text-align:right;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;margin-top:.9rem}table{border-collapse:collapse;width:100%;min-width:46rem;font-size:.82rem}
th,td{padding:.52rem .65rem;border-bottom:1px solid var(--line);white-space:nowrap;font-variant-numeric:tabular-nums}
th{text-align:left;color:var(--secondary);font-size:.74rem;font-weight:650;border-bottom:1px solid var(--baseline)}
td.n,th.n{text-align:right}.key{display:inline-flex;align-items:center;gap:.45rem}tbody tr:last-child td{border-bottom:0}
.waste-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(13rem,1fr));gap:.75rem}
.waste-item{padding:.9rem 1rem;border-left:3px solid var(--blue);background:var(--surface);border-radius:0 8px 8px 0}
.waste-item.bad{border-color:var(--orange)}.waste-item .k{color:var(--secondary);font-size:.78rem}
.waste-item .v{font-size:1.35rem;font-weight:650}.waste-item .foot{color:var(--muted);font-size:.76rem}
footer{border-top:1px solid var(--line);padding-top:1.4rem;color:var(--secondary);font-size:.82rem}
footer h3{color:var(--text);font-size:.9rem;margin:1rem 0 .35rem}footer p{margin:.3rem 0}
@media(max-width:42rem){body{padding:1.5rem .8rem 3rem}.wrap{min-width:0}
.cards,.success-grid,.waste-grid{grid-template-columns:1fr}.card{padding:1rem}.hero .v{font-size:2.35rem}
.hero .v,.scenario-value,.tile .v,.meta,.note,.sub{overflow-wrap:anywhere}
.row{grid-template-columns:6.5rem 1fr}.row .val{grid-column:2;min-width:0;text-align:left}
.in-label{display:none}.scenario-head{display:block}.token-value{text-align:left;margin-top:.25rem}}
"""

    path.write_text(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{report_title} &mdash; {esc(meta['eval_run_id'])}</title>
<style>{css}</style></head><body><div class="wrap">
<header><h1>{report_title}</h1>
<p class="meta">Run <code>{esc(meta['eval_run_id'])}</code> &middot;
agent <code>{esc(meta['agent'])}</code> &middot; model <code>{esc(meta['model'])}</code><br>
{esc(meta['attempts'])} attempts/task &middot; generated {esc(meta['generated_at'])} &middot;
agent version {version_text} &middot; {mode_label}</p><p class="note">{esc(meta['pricing_note'])}</p></header>
{coverage_banner}
<section><div class="card hero"><div class="k">{hero_label}</div>
<div class="v">{hero_value}</div>
{hero_secondary}
<div class="foot">{hero_foot}</div></div>
<div class="cards">
<div class="tile"><div class="k">Total tokens</div><div class="v">{_fmt_html_tokens(totals['n_total_tokens'])}</div>
<div class="foot">Measured run total</div></div>
{pricing_tile}
<div class="tile"><div class="k">Tasks solved</div><div class="v">{cps['tasks_solved']}</div>
<div class="foot">{cps['successful_trials']} successful trials</div></div>
<div class="tile"><div class="k">Measurement coverage</div><div class="v">{cov['measured_pct']}%</div>
<div class="foot">{coverage_foot}</div></div>
</div></section>

<section><h2><span class="num">1</span>Token composition</h2>
<p class="sub">Measured input and output token totals used by the report.</p>
<div class="cards">
<div class="tile"><div class="k">Input</div><div class="v">{_fmt_html_tokens(totals['n_input_tokens'])}</div></div>
<div class="tile"><div class="k">Output</div><div class="v">{_fmt_html_tokens(totals['n_output_tokens'])}</div></div>
</div></section>

<section><h2><span class="num">2</span>Success economics</h2>
<p class="sub">Three intentionally different views. Failed trials are isolated runs, so the successful-trial average excludes them.</p>
<div class="success-grid">{success_cards}</div>
{successful_totals}</section>

<section><h2><span class="num">3</span>Token usage by outcome</h2>
<p class="sub">{outcome_sub}</p>
<div class="card"><div class="stack">{''.join(stack_segments)}</div>
<div class="legend">{''.join(stack_legend)}</div><div class="bars">{''.join(outcome_bars)}</div>
<div class="scroll"><table><thead><tr><th>Outcome</th><th class="n">Trials</th>
<th class="n">Measured</th><th class="n">Priceable</th><th class="n">Input</th>
<th class="n">Output</th><th class="n">Total</th>
<th class="n">Avg tokens/trial</th>{outcome_cost_headers}{outcome_billed_header}
</tr></thead><tbody>{''.join(outcome_rows)}</tbody></table></div></div></section>

<section><h2><span class="num">4</span>{round_title}</h2>
<p class="sub">{round_sub}</p>
<div class="card"><div class="bars">{''.join(round_bars)}</div>
<div class="scroll"><table><thead><tr><th>Round</th><th class="n">Trials</th><th class="n">Solves</th>
<th class="n">Measured</th><th class="n">Priceable</th><th class="n">Input</th>
<th class="n">Output</th>
<th class="n">Total</th><th class="n">Avg tokens/trial</th>{round_cost_headers}
{round_billed_header}{round_value_header}</tr></thead><tbody>{''.join(round_rows)}</tbody></table></div></div></section>

<section><h2><span class="num">5</span>Attribution and waste</h2>
<p class="sub">Winning-attempt usage is attributable to a solve; all other measured usage remains retry or unsolved spend.</p>
<div class="waste-grid">
{waste_cards}
</div></section>

<section><h2><span class="num">6</span>Per task</h2>
<p class="sub">All attempts remain visible alongside the first winning attempt. A zero winning value means the task was not solved.</p>
<div class="card"><div class="scroll"><table><thead><tr><th>Task</th><th class="n">Trials</th>
<th>Solved</th><th class="n">Measured</th><th class="n">Priceable</th>
{task_headers}</tr></thead><tbody>{task_rows}</tbody></table></div></div></section>

<footer><h3>Methodology</h3><p>{esc(cov['note'])}</p>
<p><strong>Coverage quality:</strong> {quality_summary}.</p>
<p>{methodology} Missing telemetry contributes no tokens,
so incomplete-coverage totals are lower bounds.</p><h3>Metric definitions</h3>
<p><strong>Including failed retries</strong> divides all measured run usage by tasks solved at least once.
<strong>Winning attempt only</strong> uses the first successful trial per solved task.
<strong>Average successful trial</strong> includes every independently successful trial and excludes failed trials.</p></footer>
</div></body></html>
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir = pathlib.Path(args.run_dir)
    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pricing = Pricing(
        args.price_input,
        args.price_output,
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
    write_html(out_dir / "token_usage.html", report)

    cov = report["aggregates"]["coverage"]
    totals = report["aggregates"]["totals"]
    print(
        f"[token-usage] → {out_dir}/token_usage.{{json,csv,md,html}}"
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
