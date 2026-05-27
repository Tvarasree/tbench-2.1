#!/usr/bin/env python3
"""
terminal-bench v2 diagnostic reporter.

Consumes:
  - results.json  (the standardized <EVAL_RUN_ID>_results.json the harness writes)
  - task_catalog.json (the curated per-task taxonomy)

Emits a markdown report scoring the model+agent pair across:
  - upstream category × my-domain
  - capability axis (the diagnostic axis — where strengths/weaknesses live)
  - upstream difficulty
  + headline findings + per-failed-task notes + watchlist for no-grade tasks.

Design constraints:
  - Pure stdlib. Python >= 3.8. Runs identically on Linux / macOS / Windows.
  - Defensive about every input field — never crashes on a malformed JSON.
  - Catalog superset of dataset is OK; tasks missing from catalog are reported but
    don't break aggregation.
  - Multi-attempt runs (pass@k) handled via per_task.passed_attempts/total_attempts.
  - Exit code is always 0 (lifecycle-friendly for the eval-runner).

Usage:
  python report.py -i <results.json> [-c <task_catalog.json>] [-o <out.md>]
  python report.py --help
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
DEFAULT_CATALOG = SCRIPT_DIR / "task_catalog.json"

# A capability is "strong" if pass rate >= STRONG_PCT with at least MIN_N tasks,
# "weak" if pass rate <= WEAK_PCT with at least MIN_N tasks.
STRONG_PCT = 70.0
WEAK_PCT = 35.0
MIN_N_FOR_VERDICT = 3

# How many failed tasks to surface in the "investigate first" section.
TOP_FAILURES_DEFAULT = 15

# Verdict normalization map (defensive: harness uses these three; future-proof)
_KNOWN_VERDICTS = {"solved", "unsolved", "no-grade", "no_grade", "error", "timeout"}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _read_json(path: pathlib.Path) -> dict | list:
    if not path.is_file():
        raise FileNotFoundError(f"not a file: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_results(path: pathlib.Path) -> dict[str, Any]:
    """Normalize the harness results.json into a single flat dict for downstream code.

    Handles both the rich shape (metrics.additional.per_task) and the fallback
    zero-metric shape (no per_task — treated as empty run with a status note).
    """
    raw = _read_json(path)
    metrics = raw.get("metrics", {}) if isinstance(raw, dict) else {}
    additional = metrics.get("additional", {}) if isinstance(metrics, dict) else {}
    secondary = metrics.get("secondary", {}) if isinstance(metrics, dict) else {}
    per_task = additional.get("per_task", {}) if isinstance(additional, dict) else {}

    # Defensive: per_task may be missing/None/list-shaped
    if not isinstance(per_task, dict):
        per_task = {}

    # Normalize each per-task entry
    norm: dict[str, dict[str, Any]] = {}
    for tid, rec in per_task.items():
        if not isinstance(rec, dict):
            continue
        verdict = str(rec.get("verdict", "")).strip().lower().replace("_", "-")
        if verdict not in {"solved", "unsolved", "no-grade"}:
            verdict = "unknown"
        passed = int(rec.get("passed_attempts", 0) or 0)
        graded = int(rec.get("graded_attempts", 0) or 0)
        total = int(rec.get("total_attempts", 0) or 0)
        norm[tid] = {
            "verdict": verdict,
            "passed_attempts": passed,
            "graded_attempts": graded,
            "total_attempts": total,
            "pass_rate": (passed / total) if total else None,
        }

    # Cross-reference with solved/unsolved/no_grade arrays (defensive — some
    # harness paths populate the arrays but skip per_task)
    for key, verdict in (
        ("solved_tasks", "solved"),
        ("unsolved_tasks", "unsolved"),
        ("no_grade_tasks", "no-grade"),
    ):
        arr = additional.get(key, []) or []
        if not isinstance(arr, list):
            continue
        for tid in arr:
            if not isinstance(tid, str):
                continue
            if tid not in norm:
                norm[tid] = {
                    "verdict": verdict,
                    "passed_attempts": 1 if verdict == "solved" else 0,
                    "graded_attempts": 0 if verdict == "no-grade" else 1,
                    "total_attempts": 1,
                    "pass_rate": 1.0 if verdict == "solved" else (0.0 if verdict == "unsolved" else None),
                }

    return {
        "agent": str(additional.get("agent", "") or ""),
        "model": str(additional.get("model", "") or ""),
        "dataset": str(additional.get("dataset", "") or ""),
        "attempts_per_task": int(additional.get("attempts_per_task", 0) or 0),
        "harbor_exit_code": additional.get("harbor_exit_code"),
        "solved": int(secondary.get("solved", 0) or 0),
        "unsolved": int(secondary.get("unsolved", 0) or 0),
        "no_grade": int(secondary.get("no_grade", 0) or 0),
        "total": int(secondary.get("total", 0) or 0),
        "solve_rate_pct": float(secondary.get("solve_rate_pct", 0) or 0),
        "fallback_status": additional.get("status") if isinstance(additional, dict) else None,
        "fallback_reason": additional.get("reason") if isinstance(additional, dict) else None,
        "per_task": norm,
    }


def load_catalog(path: pathlib.Path) -> dict[str, Any]:
    raw = _read_json(path)
    if not isinstance(raw, dict):
        raise ValueError("catalog must be a JSON object at the top level")
    tasks = raw.get("tasks", []) or []
    by_id: dict[str, dict[str, Any]] = {}
    for entry in tasks:
        if not isinstance(entry, dict):
            continue
        tid = entry.get("id")
        if isinstance(tid, str) and tid:
            by_id[tid] = entry
    return {
        "schema_version": raw.get("schema_version"),
        "source": raw.get("source", {}),
        "capability_taxonomy": raw.get("capability_taxonomy", {}),
        "by_id": by_id,
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate(results: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    """Build counters per upstream category, my-domain, capability, difficulty."""
    by_upstream_cat: dict[str, dict[str, int]] = defaultdict(lambda: {"solved": 0, "n": 0})
    by_my_domain: dict[str, dict[str, int]] = defaultdict(lambda: {"solved": 0, "n": 0})
    by_capability: dict[str, dict[str, int]] = defaultdict(lambda: {"solved": 0, "n": 0})
    by_difficulty: dict[str, dict[str, int]] = defaultdict(lambda: {"solved": 0, "n": 0})

    catalog_by_id = catalog["by_id"]
    in_catalog: list[str] = []
    not_in_catalog: list[str] = []

    for tid, rec in results["per_task"].items():
        meta = catalog_by_id.get(tid)
        if meta is None:
            not_in_catalog.append(tid)
            continue
        in_catalog.append(tid)

        upstream = meta.get("upstream") or {}
        taxonomy = meta.get("taxonomy") or {}
        solved = 1 if rec["verdict"] == "solved" else 0
        graded = rec["verdict"] in ("solved", "unsolved")

        cat = (upstream.get("category") or "uncategorized").strip()
        if graded:
            by_upstream_cat[cat]["n"] += 1
            by_upstream_cat[cat]["solved"] += solved

        # Parse my-domain — it's "<upstream> (<my domain> — <prose>)"
        # Extract just the my-domain segment for aggregation.
        my_domain = _parse_my_domain(taxonomy.get("domain") or "") or cat
        if graded:
            by_my_domain[my_domain]["n"] += 1
            by_my_domain[my_domain]["solved"] += solved

        for cap in (taxonomy.get("capabilities") or []):
            if not isinstance(cap, str) or not cap:
                continue
            if graded:
                by_capability[cap]["n"] += 1
                by_capability[cap]["solved"] += solved

        diff = (upstream.get("difficulty") or "unknown").strip().lower()
        if graded:
            by_difficulty[diff]["n"] += 1
            by_difficulty[diff]["solved"] += solved

    # Also track no-grade tasks separately — they're "watchlist" not "failures"
    no_grade = [tid for tid, rec in results["per_task"].items() if rec["verdict"] == "no-grade"]
    failed = [tid for tid, rec in results["per_task"].items() if rec["verdict"] == "unsolved"]
    solved = [tid for tid, rec in results["per_task"].items() if rec["verdict"] == "solved"]
    unknown = [tid for tid, rec in results["per_task"].items() if rec["verdict"] not in {"solved", "unsolved", "no-grade"}]

    return {
        "by_upstream_cat": dict(by_upstream_cat),
        "by_my_domain": dict(by_my_domain),
        "by_capability": dict(by_capability),
        "by_difficulty": dict(by_difficulty),
        "in_catalog": in_catalog,
        "not_in_catalog": not_in_catalog,
        "solved": solved,
        "failed": failed,
        "no_grade": no_grade,
        "unknown": unknown,
    }


def _parse_my_domain(domain_str: str) -> str:
    """Extract the my-domain segment from '<upstream> (<my domain> — <prose>)'."""
    if not domain_str or "(" not in domain_str:
        return domain_str
    inside = domain_str.split("(", 1)[1].rsplit(")", 1)[0]
    # split on the en-dash or hyphen — prefer the segment before it
    for sep in ("—", " — ", " - ", " -- "):
        if sep in inside:
            return inside.split(sep, 1)[0].strip()
    return inside.strip()


# ---------------------------------------------------------------------------
# Diagnostics — derive headline findings from aggregates
# ---------------------------------------------------------------------------

def headline_findings(agg: dict[str, Any], results: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    caps = agg["by_capability"]

    strong = [(c, _pct(v)) for c, v in caps.items() if v["n"] >= MIN_N_FOR_VERDICT and _pct(v) >= STRONG_PCT]
    weak = [(c, _pct(v)) for c, v in caps.items() if v["n"] >= MIN_N_FOR_VERDICT and _pct(v) <= WEAK_PCT]
    cliffs = [c for c, v in caps.items() if v["n"] >= MIN_N_FOR_VERDICT and v["solved"] == 0]

    strong.sort(key=lambda x: -x[1])
    weak.sort(key=lambda x: x[1])

    if strong:
        top = strong[:3]
        findings.append(
            "Strong capabilities (≥{:.0f}% pass with n≥{}): {}.".format(
                STRONG_PCT, MIN_N_FOR_VERDICT,
                ", ".join(f"**{c}** ({p:.0f}%)" for c, p in top),
            )
        )
    if cliffs:
        findings.append(
            "Critical gap — zero passes on capability(ies): "
            + ", ".join(f"**{c}**" for c in cliffs)
            + ". Treat as a hard ceiling, not noise."
        )
    if weak:
        bot = weak[:4]
        findings.append(
            "Weak capabilities (≤{:.0f}% pass with n≥{}): {}.".format(
                WEAK_PCT, MIN_N_FOR_VERDICT,
                ", ".join(f"**{c}** ({p:.0f}%)" for c, p in bot),
            )
        )

    # Language asymmetry
    lp = caps.get("language_python", {"solved": 0, "n": 0})
    lc = caps.get("language_c_cpp", {"solved": 0, "n": 0})
    lo = caps.get("language_other", {"solved": 0, "n": 0})
    pairs = [
        ("Python", lp), ("C/C++", lc), ("Other", lo),
    ]
    pairs_ranked = [(name, _pct(v)) for name, v in pairs if v["n"] >= MIN_N_FOR_VERDICT]
    if len(pairs_ranked) >= 2:
        pairs_ranked.sort(key=lambda x: -x[1])
        gap = pairs_ranked[0][1] - pairs_ranked[-1][1]
        if gap >= 20:
            findings.append(
                f"Language asymmetry: {pairs_ranked[0][0]} {pairs_ranked[0][1]:.0f}% "
                f"vs {pairs_ranked[-1][0]} {pairs_ranked[-1][1]:.0f}% — "
                f"{gap:.0f}pp gap suggests language-specific familiarity weakness."
            )

    # Difficulty cliff
    diffs = agg["by_difficulty"]
    e = diffs.get("easy", {"solved": 0, "n": 0})
    h = diffs.get("hard", {"solved": 0, "n": 0})
    if e["n"] >= MIN_N_FOR_VERDICT and h["n"] >= MIN_N_FOR_VERDICT:
        ep, hp = _pct(e), _pct(h)
        if ep - hp >= 40:
            findings.append(
                f"Difficulty cliff: easy {ep:.0f}% → hard {hp:.0f}% "
                f"({ep - hp:.0f}pp drop). Suggests one-shot success but inability to sustain on multi-step tasks."
            )

    # No-grade rate sanity check
    if results["total"] and results["no_grade"] / max(results["total"], 1) >= 0.10:
        findings.append(
            f"High no-grade rate ({results['no_grade']}/{results['total']} = "
            f"{100 * results['no_grade'] / results['total']:.0f}%). Investigate harness/environment "
            f"issues, not just model capability — these may not be real failures."
        )

    if not findings:
        findings.append("Insufficient signal for headline findings (run too small or catalog mismatch).")

    return findings


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _pct(v: dict[str, int]) -> float:
    return (100.0 * v["solved"] / v["n"]) if v["n"] else 0.0


def _bar(p: float, width: int = 20) -> str:
    filled = int(round(p / 100.0 * width))
    return "█" * filled + "·" * (width - filled)


def _verdict_marker(pct: float, n: int) -> str:
    if n < MIN_N_FOR_VERDICT:
        return "○"
    if pct >= STRONG_PCT:
        return "✓"
    if pct <= WEAK_PCT:
        return "✗"
    return "·"


def render_table(title: str, buckets: dict[str, dict[str, int]]) -> str:
    if not buckets:
        return f"### {title}\n\n_(no graded tasks in this run match the catalog)_\n"
    rows = sorted(buckets.items(), key=lambda kv: (-_pct(kv[1]), -kv[1]["n"]))
    width = max(20, *(len(k) for k in buckets.keys()))
    lines = [f"### {title}", ""]
    lines.append(f"| {'bucket'.ljust(width)} | passed | n  | pass% | bar                  |   |")
    lines.append(f"| {'-' * width} | -----: | -: | ----: | -------------------- | - |")
    for k, v in rows:
        p = _pct(v)
        lines.append(
            f"| {k.ljust(width)} | {v['solved']:>6} | {v['n']:>2} | {p:>4.0f}% "
            f"| {_bar(p)} | {_verdict_marker(p, v['n'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_failures(results: dict[str, Any], catalog: dict[str, Any], top_n: int) -> str:
    failed = [tid for tid, rec in results["per_task"].items() if rec["verdict"] == "unsolved"]
    if not failed:
        return "### Failed-task detail\n\n_(no graded failures)_\n"
    failed.sort()
    lines = ["### Failed-task detail", "",
             f"_{len(failed)} graded failures; showing first {min(top_n, len(failed))}._", ""]
    for tid in failed[:top_n]:
        meta = catalog["by_id"].get(tid) or {}
        upstream = meta.get("upstream") or {}
        taxonomy = meta.get("taxonomy") or {}
        one_liner = meta.get("one_liner") or "_(not in catalog)_"
        note = meta.get("diagnostic_note") or "_(no diagnostic note in catalog)_"
        rec = results["per_task"][tid]
        lines.append(f"#### `{tid}`  · {upstream.get('difficulty','?')} · {upstream.get('category','?')}")
        lines.append("")
        lines.append(f"> {one_liner}")
        lines.append("")
        if taxonomy.get("capabilities"):
            lines.append(f"**Capabilities:** {', '.join(taxonomy['capabilities'])}  ")
        if meta.get("failure_modes"):
            lines.append(f"**Likely failure modes:** {', '.join(meta['failure_modes'])}  ")
        lines.append(
            f"**Attempts:** {rec['passed_attempts']}/{rec['total_attempts']} passed "
            f"({rec['graded_attempts']} graded)"
        )
        lines.append("")
        lines.append(note.strip())
        lines.append("")
    if len(failed) > top_n:
        lines.append(f"_{len(failed) - top_n} more failures elided. Pass `--top-failures {len(failed)}` to see all._")
        lines.append("")
    return "\n".join(lines)


def render_watchlist(results: dict[str, Any]) -> str:
    no_grade = [tid for tid, rec in results["per_task"].items() if rec["verdict"] == "no-grade"]
    if not no_grade:
        return ""
    lines = ["### Watchlist — no-grade tasks", "",
             "Tasks the harness couldn't grade (verifier failed, container crashed, "
             "agent timed out before producing artifacts). These are environment/run "
             "concerns, not model failures.", ""]
    for tid in sorted(no_grade):
        lines.append(f"- `{tid}`")
    lines.append("")
    return "\n".join(lines)


def render_unknown(agg: dict[str, Any]) -> str:
    if not agg["not_in_catalog"]:
        return ""
    lines = [
        "### Tasks not in catalog",
        "",
        f"{len(agg['not_in_catalog'])} task(s) in the results.json are missing from "
        "task_catalog.json. They are counted in the top-line solve rate but excluded "
        "from category/capability/difficulty aggregates. Update the catalog to include "
        "these for full diagnostic coverage:", "",
    ]
    for tid in sorted(agg["not_in_catalog"]):
        lines.append(f"- `{tid}`")
    lines.append("")
    return "\n".join(lines)


def render_report(results: dict[str, Any], catalog: dict[str, Any], top_failures: int) -> str:
    agg = aggregate(results, catalog)
    findings = headline_findings(agg, results)

    n_total = results["total"] or len(results["per_task"])
    n_solved = results["solved"]
    n_unsolved = results["unsolved"]
    n_no_grade = results["no_grade"]
    rate = results["solve_rate_pct"]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    header = [
        "# Terminal-Bench v2 — Diagnostic Report",
        "",
        f"**Generated:** {stamp}  ",
        f"**Model:** `{results['model'] or 'unknown'}`  ",
        f"**Agent:** `{results['agent'] or 'unknown'}`  ",
        f"**Dataset:** `{results['dataset'] or 'unknown'}`  ",
        f"**Attempts/task (pass@k):** {results['attempts_per_task'] or '?'}  ",
        f"**Catalog source:** {catalog['source'].get('upstream_repo','?')} "
        f"@ `{catalog['source'].get('branch','?')}` (fetched {catalog['source'].get('fetched','?')})",
        "",
        "## Headline",
        "",
        f"**Solved:** {n_solved} / {n_total}   ({rate:.1f}%)",
        f"**Unsolved:** {n_unsolved}   **No-grade:** {n_no_grade}",
        "",
    ]
    if results.get("fallback_status") == "no-results":
        header.append(
            f"> ⚠ Fallback results detected (status: `{results['fallback_status']}`, "
            f"reason: `{results['fallback_reason']}`). The harness wrote a zero-metric "
            "fallback — this report has no per-task signal. Investigate the run logs.")
        header.append("")

    findings_section = ["## Findings", ""] + [f"- {f}" for f in findings] + [""]

    body = "\n".join(
        header
        + findings_section
        + ["## Aggregate breakdowns", ""]
        + [render_table("By upstream category", agg["by_upstream_cat"])]
        + [render_table("By my-domain (cross-check axis)", agg["by_my_domain"])]
        + [render_table("By capability (diagnostic axis)", agg["by_capability"])]
        + [render_table("By upstream difficulty", agg["by_difficulty"])]
        + ["## Failures and watchlist", ""]
        + [render_failures(results, catalog, top_failures)]
        + [render_watchlist(results)]
        + [render_unknown(agg)]
    )
    return body


# ---------------------------------------------------------------------------
# JSON dump (machine-readable companion)
# ---------------------------------------------------------------------------

def dump_json(results: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    agg = aggregate(results, catalog)
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run": {
            "model": results["model"],
            "agent": results["agent"],
            "dataset": results["dataset"],
            "attempts_per_task": results["attempts_per_task"],
            "solved": results["solved"],
            "unsolved": results["unsolved"],
            "no_grade": results["no_grade"],
            "total": results["total"],
            "solve_rate_pct": results["solve_rate_pct"],
        },
        "by_upstream_category": {k: {"solved": v["solved"], "n": v["n"], "pct": _pct(v)}
                                 for k, v in agg["by_upstream_cat"].items()},
        "by_my_domain": {k: {"solved": v["solved"], "n": v["n"], "pct": _pct(v)}
                         for k, v in agg["by_my_domain"].items()},
        "by_capability": {k: {"solved": v["solved"], "n": v["n"], "pct": _pct(v)}
                          for k, v in agg["by_capability"].items()},
        "by_difficulty": {k: {"solved": v["solved"], "n": v["n"], "pct": _pct(v)}
                          for k, v in agg["by_difficulty"].items()},
        "tasks_solved": agg["solved"],
        "tasks_failed": agg["failed"],
        "tasks_no_grade": agg["no_grade"],
        "tasks_unknown_verdict": agg["unknown"],
        "tasks_not_in_catalog": agg["not_in_catalog"],
        "findings": headline_findings(agg, results),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="report.py",
        description="Diagnostic report for a terminal-bench v2 run.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-i", "--input", required=True,
                   help="Path to <EVAL_RUN_ID>_results.json")
    p.add_argument("-c", "--catalog", default=str(DEFAULT_CATALOG),
                   help=f"Path to task_catalog.json (default: {DEFAULT_CATALOG})")
    p.add_argument("-o", "--output", default=None,
                   help="Path to write markdown report (default: <input>.report.md)")
    p.add_argument("--format", choices=("md", "json", "both"), default="both",
                   help="Output format(s). Default: both md and json next to --output")
    p.add_argument("--top-failures", type=int, default=TOP_FAILURES_DEFAULT,
                   help=f"Number of failed tasks to detail (default: {TOP_FAILURES_DEFAULT})")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress stderr progress lines.")
    args = p.parse_args(argv)

    def say(msg: str) -> None:
        if not args.quiet:
            print(msg, file=sys.stderr)

    in_path = pathlib.Path(args.input).expanduser().resolve()
    cat_path = pathlib.Path(args.catalog).expanduser().resolve()

    try:
        results = load_results(in_path)
    except Exception as exc:
        print(f"[report.py] failed to load results: {exc}", file=sys.stderr)
        return 0  # don't fail the eval-runner
    try:
        catalog = load_catalog(cat_path)
    except Exception as exc:
        print(f"[report.py] failed to load catalog: {exc}", file=sys.stderr)
        # Continue with an empty catalog; downstream sections degrade gracefully.
        catalog = {"schema_version": None, "source": {}, "capability_taxonomy": {}, "by_id": {}}

    out_md = pathlib.Path(args.output) if args.output else in_path.with_suffix(".report.md")
    if out_md.suffix.lower() == ".md":
        out_json = out_md.with_suffix(".json")
    else:
        out_json = out_md.with_name(out_md.name + ".json")
    out_md.parent.mkdir(parents=True, exist_ok=True)

    if args.format in ("md", "both"):
        md = render_report(results, catalog, args.top_failures)
        out_md.write_text(md, encoding="utf-8")
        say(f"[report.py] markdown → {out_md}")

    if args.format in ("json", "both"):
        out_json.write_text(json.dumps(dump_json(results, catalog), indent=2), encoding="utf-8")
        say(f"[report.py] json     → {out_json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
