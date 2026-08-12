#!/usr/bin/env python3
"""Inspect Harbor job completion and persist automatic-recovery metadata."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class JobState:
    planned: int
    completed: int
    valid_completed: int
    cancelled: int
    pending: int
    running: int
    complete: bool
    incomplete_trials: tuple[str, ...]


def _json(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def inspect_job(job_dir: pathlib.Path, *, planned: int) -> JobState:
    completed = cancelled = 0
    incomplete: list[str] = []

    if job_dir.is_dir():
        for trial_dir in sorted(job_dir.iterdir()):
            if not trial_dir.is_dir() or trial_dir.name.startswith("."):
                continue
            result = _json(trial_dir / "result.json")
            if result is None:
                incomplete.append(trial_dir.name)
                continue
            completed += 1
            exception = result.get("exception_info")
            if isinstance(exception, dict) and exception.get("exception_type") == "CancelledError":
                cancelled += 1
                incomplete.append(trial_dir.name)

    valid_completed = completed - cancelled
    pending = max(planned - valid_completed, 0)
    root_result = _json(job_dir / "result.json") if job_dir.is_dir() else None
    stats = root_result.get("stats") if isinstance(root_result, dict) else None
    running = int(stats.get("n_running_trials", 0) or 0) if isinstance(stats, dict) else 0
    complete = (
        planned > 0
        and completed == planned
        and valid_completed == planned
        and cancelled == 0
        and pending == 0
        and running == 0
    )
    return JobState(
        planned=planned,
        completed=completed,
        valid_completed=valid_completed,
        cancelled=cancelled,
        pending=pending,
        running=running,
        complete=complete,
        incomplete_trials=tuple(incomplete),
    )


def build_recovery_metadata(
    *, initial: JobState, final: JobState, invocation_exit_codes: list[int]
) -> dict[str, Any]:
    return {
        "status": "complete" if final.complete else "incomplete",
        "planned_trials": final.planned,
        "completed_trials": final.completed,
        "valid_completed_trials": final.valid_completed,
        "pending_trials": final.pending,
        "running_trials": final.running,
        "cancelled_trials": final.cancelled,
        "initial_harbor_exit_code": invocation_exit_codes[0]
        if invocation_exit_codes
        else None,
        "invocation_exit_codes": invocation_exit_codes,
        "automatic_resume_count": max(len(invocation_exit_codes) - 1, 0),
        "recovered_trials": max(final.valid_completed - initial.valid_completed, 0),
        "remaining_incomplete_trials": final.pending,
        "incomplete_trial_sample": list(final.incomplete_trials[:20]),
    }


def write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    inspect_parser = commands.add_parser("inspect")
    inspect_parser.add_argument("--job-dir", required=True, type=pathlib.Path)
    inspect_parser.add_argument("--planned", required=True, type=int)

    report_parser = commands.add_parser("report")
    report_parser.add_argument("--job-dir", required=True, type=pathlib.Path)
    report_parser.add_argument("--planned", required=True, type=int)
    report_parser.add_argument("--initial-state", required=True, type=pathlib.Path)
    report_parser.add_argument("--exit-codes", required=True)
    report_parser.add_argument("--output", required=True, type=pathlib.Path)

    args = parser.parse_args()
    if args.command == "inspect":
        print(json.dumps(asdict(inspect_job(args.job_dir, planned=args.planned))))
        return 0

    initial_payload = json.loads(args.initial_state.read_text(encoding="utf-8"))
    initial_payload["incomplete_trials"] = tuple(
        initial_payload.get("incomplete_trials", [])
    )
    initial = JobState(**initial_payload)
    final = inspect_job(args.job_dir, planned=args.planned)
    exit_codes = [int(value) for value in args.exit_codes.split(",") if value != ""]
    payload = build_recovery_metadata(
        initial=initial, final=final, invocation_exit_codes=exit_codes
    )
    write_json(args.output, payload)
    print(json.dumps(payload))
    return 0 if final.complete and exit_codes and exit_codes[-1] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
