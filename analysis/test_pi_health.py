#!/usr/bin/env python3
"""Systemic health-gate tests for Terminal-Bench Pi runs."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import pi_health  # noqa: E402


def make_trial(
    run_dir: pathlib.Path,
    name: str,
    *,
    message: dict | None = None,
    raw_output: str = "",
    exception_type: str | None = None,
) -> None:
    trial = run_dir / name
    (trial / "agent").mkdir(parents=True)
    if message is not None:
        (trial / "agent" / "pi.txt").write_text(
            json.dumps({"type": "message_end", "message": message}) + "\n",
            encoding="utf-8",
        )
    else:
        (trial / "agent" / "pi.txt").write_text(raw_output, encoding="utf-8")
    result = {
        "exception_info": (
            {"exception_type": exception_type, "exception_message": "failed"}
            if exception_type
            else None
        )
    }
    (trial / "result.json").write_text(json.dumps(result), encoding="utf-8")


def active_message() -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": "Attempt completed"}],
        "stopReason": "stop",
        "usage": {"input": 100, "output": 10},
    }


def auth_error_message() -> dict:
    return {
        "role": "assistant",
        "content": [],
        "stopReason": "error",
        "errorMessage": "401 Incorrect API key provided",
        "usage": {"input": 0, "output": 0},
    }


class PiHealthTest(unittest.TestCase):
    def test_all_authentication_errors_are_systemically_invalid(self) -> None:
        """Catches publishing an all-401 run as a legitimate benchmark zero."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_trial(run_dir, "alpha__1", message=auth_error_message())
            make_trial(run_dir, "beta__1", message=auth_error_message())

            report = pi_health.analyze_run(run_dir)

        self.assertEqual(report["status"], "invalid")
        self.assertEqual(report["total_attempts"], 2)
        self.assertEqual(report["active_attempts"], 0)
        self.assertEqual(report["api_error_attempts"], 2)

    def test_all_cli_errors_are_systemically_invalid(self) -> None:
        """Catches a positional-prompt parser failure across every attempt."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_trial(
                run_dir,
                "alpha__1",
                raw_output="Error: Unknown option: - task\n",
                exception_type="NonZeroAgentExitCodeError",
            )

            report = pi_health.analyze_run(run_dir)

        self.assertEqual(report["status"], "invalid")
        self.assertEqual(report["active_attempts"], 0)
        self.assertEqual(report["exception_attempts"], 1)

    def test_real_model_activity_is_healthy_even_when_reward_is_zero(self) -> None:
        """Catches turning genuine model failure into infrastructure failure."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_trial(run_dir, "alpha__1", message=active_message())
            verifier = run_dir / "alpha__1" / "verifier"
            verifier.mkdir()
            (verifier / "reward.txt").write_text("0", encoding="utf-8")

            report = pi_health.analyze_run(run_dir)

        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["active_attempts"], 1)
        self.assertEqual(report["total_tokens"], 110)

    def test_mixed_run_reports_errors_but_does_not_fail_systemically(self) -> None:
        """Catches requiring every independent trial to succeed operationally."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_trial(run_dir, "alpha__1", message=active_message())
            make_trial(run_dir, "beta__1", message=auth_error_message())

            report = pi_health.analyze_run(run_dir)

        self.assertEqual(report["status"], "degraded")
        self.assertEqual(report["active_attempts"], 1)
        self.assertEqual(report["api_error_attempts"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
