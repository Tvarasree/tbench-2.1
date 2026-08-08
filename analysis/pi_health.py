#!/usr/bin/env python3
"""Detect systemic Pi harness failures without treating reward-zero as invalid."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any


ADAPTER_DIR = pathlib.Path(__file__).resolve().parents[1] / "adapter"
if str(ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(ADAPTER_DIR))

from pi_harbor_agent.transcript import analyze_transcript  # noqa: E402


def _exception_info(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("exception_info")
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def analyze_run(run_dir: pathlib.Path) -> dict[str, Any]:
    trials = (
        sorted(
            path
            for path in run_dir.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        )
        if run_dir.is_dir()
        else []
    )
    active_attempts = 0
    api_error_attempts = 0
    exception_attempts = 0
    total_tokens = 0
    failures: list[dict[str, Any]] = []

    for trial in trials:
        summary = analyze_transcript(trial / "agent" / "pi.txt")
        exception = _exception_info(trial / "result.json")
        active_attempts += int(summary.active)
        total_tokens += summary.input_tokens + summary.output_tokens
        if summary.final_stop_reason in {"error", "aborted"}:
            api_error_attempts += 1
        if exception is not None:
            exception_attempts += 1
        if not summary.active:
            failures.append(
                {
                    "trial": trial.name,
                    "stop_reason": summary.final_stop_reason,
                    "error": summary.final_error_message,
                    "exception_type": (
                        exception.get("exception_type") if exception else None
                    ),
                }
            )

    total_attempts = len(trials)
    if total_attempts == 0:
        status = "no-trials"
    elif active_attempts == 0:
        status = "invalid"
    elif active_attempts < total_attempts:
        status = "degraded"
    else:
        status = "healthy"

    return {
        "status": status,
        "total_attempts": total_attempts,
        "active_attempts": active_attempts,
        "inactive_attempts": total_attempts - active_attempts,
        "api_error_attempts": api_error_attempts,
        "exception_attempts": exception_attempts,
        "total_tokens": total_tokens,
        "failures": failures,
    }


def write_report(path: pathlib.Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()

    report = analyze_run(args.run_dir)
    write_report(args.output, report)
    print(
        "[pi-health] "
        f"{report['status']} | active {report['active_attempts']}/"
        f"{report['total_attempts']} | API errors {report['api_error_attempts']} | "
        f"exceptions {report['exception_attempts']}"
    )
    if report["status"] == "no-trials":
        return 3
    if report["status"] == "invalid":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
