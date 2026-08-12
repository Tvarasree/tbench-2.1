#!/usr/bin/env python3
"""Build the standardized eval-dashboard result from Harbor trial artifacts."""
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


def _load_tasks(run_dir: pathlib.Path) -> dict[str, dict[str, int]]:
    tasks: dict[str, dict[str, int]] = {}
    if not run_dir.is_dir():
        return tasks

    for trial in sorted(
        path
        for path in run_dir.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ):
        task_name = trial.name.split("__", 1)[0]
        record = tasks.setdefault(
            task_name,
            {"attempts": 0, "graded": 0, "passed": 0, "official_successes": 0},
        )
        record["attempts"] += 1
        reward_path = trial / "verifier" / "reward.txt"
        if not reward_path.is_file():
            continue
        try:
            reward = float(reward_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        record["graded"] += 1
        record["official_successes"] += int(reward > 0)
        record["passed"] += int(reward >= 1)
    return tasks


def _token_usage_headline(path: pathlib.Path) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "unavailable", "reason": "token_usage.json not written"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        aggregates = data["aggregates"]
        coverage = aggregates["coverage"]
        totals = aggregates["totals"]
        success = aggregates["cost_per_success"]
        waste = aggregates["waste"]
        meta = data.get("meta", {})
        return {
            "status": "ok",
            "priced": meta.get("priced", False),
            "agent_versions": meta.get("agent_versions", []),
            "pricing_note": meta.get("pricing_note", ""),
            "measured_attempts": coverage["measured_attempts"],
            "priceable_attempts": coverage.get("priceable_attempts", 0),
            "total_attempts": coverage["total_attempts"],
            "measured_pct": coverage["measured_pct"],
            "measurement_quality": coverage.get("quality_counts", {}),
            "coverage_note": coverage["note"],
            "n_input_tokens": totals["n_input_tokens"],
            "n_cache_tokens": totals["n_cache_tokens"],
            "n_output_tokens": totals["n_output_tokens"],
            "n_total_tokens": totals["n_total_tokens"],
            "cost_usd_billed": totals["cost_usd_billed"] or None,
            "cost_usd_priced": totals["cost_usd_priced"] or None,
            "cost_usd_per_solve_including_failed_retries": success[
                "cost_usd_per_solve_including_failed_retries"
            ],
            "cost_usd_per_solve_winning_attempt_only": success[
                "cost_usd_per_solve_winning_attempt_only"
            ],
            "successful_trials": success.get("successful_trials", 0),
            "measured_successful_trials": success.get(
                "measured_successful_trials", 0
            ),
            "tokens_successful_trials": success.get("tokens_successful_trials", 0),
            "avg_tokens_per_successful_trial": success.get(
                "avg_tokens_per_successful_trial"
            ),
            "priceable_successful_trials": success.get(
                "priceable_successful_trials", 0
            ),
            "cost_usd_successful_trials": success.get(
                "cost_usd_successful_trials"
            ),
            "avg_cost_usd_per_successful_trial": success.get(
                "avg_cost_usd_per_successful_trial"
            ),
            "cost_usd_wasted": waste["cost_usd_wasted"],
            "wasted_pct": waste["wasted_pct"],
        }
    except Exception as exc:  # noqa: BLE001 - result generation must stay defensive
        return {"status": "unreadable", "reason": repr(exc)}


def _agent_health(path: pathlib.Path | None) -> dict[str, Any]:
    if path is None:
        return {"status": "not-applicable"}
    if not path.is_file():
        return {"status": "unavailable", "reason": "health report not written"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - result generation stays defensive
        return {"status": "unreadable", "reason": repr(exc)}
    if not isinstance(value, dict):
        return {
            "status": "unreadable",
            "reason": "health report is not a JSON object",
        }
    failures = value.pop("failures", None)
    if isinstance(failures, list):
        value["failure_examples"] = failures[:10]
        value["failure_details_artifact"] = "pi_health.json"
    return value


def _recovery(path: pathlib.Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {"status": "unavailable", "reason": "harbor_recovery.json not written"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - result generation stays defensive
        return {"status": "unreadable", "reason": repr(exc)}
    return value if isinstance(value, dict) else {
        "status": "unreadable",
        "reason": "recovery report is not a JSON object",
    }


def build_results(
    *,
    run_dir: pathlib.Path,
    token_usage_path: pathlib.Path,
    agent: str,
    model: str,
    dataset: str,
    attempts: int,
    selected_tasks: int,
    harbor_exit_code: int,
    agent_health_path: pathlib.Path | None = None,
    recovery_path: pathlib.Path | None = None,
) -> dict[str, Any]:
    recovery = _recovery(recovery_path)
    tasks = _load_tasks(run_dir)
    solved = sorted(name for name, record in tasks.items() if record["passed"] >= 1)
    graded_tasks = [name for name, record in tasks.items() if record["graded"] >= 1]
    unsolved = sorted(name for name in graded_tasks if name not in solved)
    no_grade = sorted(
        name for name, record in tasks.items() if record["graded"] == 0
    )

    n_solved = len(solved)
    n_total = selected_tasks if selected_tasks else len(tasks)
    solve_rate = round(100.0 * n_solved / n_total, 2) if n_total else 0.0

    successful_trials = sum(record["official_successes"] for record in tasks.values())
    planned_trials = n_total * attempts
    trial_accuracy = (
        round(100.0 * successful_trials / planned_trials, 2)
        if planned_trials
        else 0.0
    )

    solved_task_ratios = {
        name: tasks[name]["passed"] / attempts
        for name in solved
        if attempts > 0
    }
    solved_task_rates = {
        name: round(100.0 * ratio, 2)
        for name, ratio in solved_task_ratios.items()
    }
    repeatability = (
        round(
            100.0 * sum(solved_task_ratios.values()) / len(solved_task_ratios),
            2,
        )
        if solved_task_ratios
        else None
    )

    return {
        "metrics": {
            "main": {"name": "Solved", "value": n_solved},
            "secondary": {
                "solved": n_solved,
                "unsolved": len(unsolved),
                "no_grade": len(no_grade),
                "total": n_total,
                "solve_rate_pct": solve_rate,
                "successful_trials": successful_trials,
                "planned_trials": planned_trials,
                "trial_accuracy_pct": trial_accuracy,
            },
            "additional": {
                "status": recovery.get("status", "unavailable"),
                "agent": agent,
                "model": model,
                "dataset": dataset,
                "attempts_per_task": attempts,
                "harbor_exit_code": harbor_exit_code,
                "harbor_recovery": recovery,
                "agent_health": _agent_health(agent_health_path),
                "token_usage": _token_usage_headline(token_usage_path),
                "solved_task_trial_success_pct": repeatability,
                "solved_task_trial_success": {
                    "solved_tasks": n_solved,
                    "successful_trials": sum(tasks[name]["passed"] for name in solved),
                    "planned_trials": n_solved * attempts,
                    "per_task_pct": solved_task_rates,
                },
                "solved_tasks": solved,
                "unsolved_tasks": unsolved,
                "no_grade_tasks": no_grade,
                "per_task": {
                    name: {
                        "passed_attempts": record["passed"],
                        "graded_attempts": record["graded"],
                        "total_attempts": record["attempts"],
                        "planned_attempts": attempts,
                        "trial_success_pct": (
                            round(100.0 * record["passed"] / attempts, 2)
                            if attempts
                            else None
                        ),
                        "verdict": (
                            "solved"
                            if record["passed"] >= 1
                            else "unsolved"
                            if record["graded"] >= 1
                            else "no-grade"
                        ),
                    }
                    for name, record in sorted(tasks.items())
                },
            },
        }
    }


def write_results(path: pathlib.Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(result, indent=2), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=pathlib.Path)
    parser.add_argument("--token-usage", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--agent", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--dataset", default="")
    parser.add_argument("--attempts", required=True, type=int)
    parser.add_argument("--selected-tasks", required=True, type=int)
    parser.add_argument("--harbor-exit-code", required=True, type=int)
    parser.add_argument("--agent-health", type=pathlib.Path)
    parser.add_argument("--recovery", type=pathlib.Path)
    args = parser.parse_args()

    result = build_results(
        run_dir=args.run_dir,
        token_usage_path=args.token_usage,
        agent=args.agent,
        model=args.model,
        dataset=args.dataset,
        attempts=args.attempts,
        selected_tasks=args.selected_tasks,
        harbor_exit_code=args.harbor_exit_code,
        agent_health_path=args.agent_health,
        recovery_path=args.recovery,
    )
    write_results(args.output, result)
    metrics = result["metrics"]
    secondary = metrics["secondary"]
    print(f"[ok] results -> {args.output}")
    print(
        f"     solved {secondary['solved']}/{secondary['total']} "
        f"({secondary['solve_rate_pct']}%) | trial accuracy "
        f"{secondary['successful_trials']}/{secondary['planned_trials']} "
        f"({secondary['trial_accuracy_pct']}%)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
