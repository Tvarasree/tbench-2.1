#!/usr/bin/env python3
"""Tests for Harbor job completion and recovery accounting."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import job_recovery


def write_trial(
    job_dir: pathlib.Path, name: str, *, exception_type: str | None = None
) -> None:
    trial = job_dir / name
    trial.mkdir(parents=True)
    (trial / "config.json").write_text("{}", encoding="utf-8")
    payload = {
        "trial_name": name,
        "exception_info": (
            {"exception_type": exception_type} if exception_type else None
        ),
    }
    (trial / "result.json").write_text(json.dumps(payload), encoding="utf-8")


class InspectJobTest(unittest.TestCase):
    def test_cancelled_results_are_recoverable_not_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = pathlib.Path(tmp)
            write_trial(job_dir, "alpha__one")
            write_trial(job_dir, "beta__two", exception_type="CancelledError")
            (job_dir / "gamma__partial").mkdir()
            (job_dir / ".sources").mkdir()

            state = job_recovery.inspect_job(job_dir, planned=4)

        self.assertEqual(state.completed, 2)
        self.assertEqual(state.cancelled, 1)
        self.assertEqual(state.valid_completed, 1)
        self.assertEqual(state.pending, 3)
        self.assertFalse(state.complete)
        self.assertEqual(state.incomplete_trials, ("beta__two", "gamma__partial"))

    def test_reward_zero_trial_is_still_a_completed_trial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = pathlib.Path(tmp)
            write_trial(job_dir, "alpha__one")
            verifier = job_dir / "alpha__one" / "verifier"
            verifier.mkdir()
            (verifier / "reward.txt").write_text("0", encoding="utf-8")

            state = job_recovery.inspect_job(job_dir, planned=1)

        self.assertTrue(state.complete)
        self.assertEqual(state.valid_completed, 1)

    def test_stale_root_running_count_prevents_complete_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = pathlib.Path(tmp)
            write_trial(job_dir, "alpha__one")
            (job_dir / "result.json").write_text(
                json.dumps({"stats": {"n_running_trials": 1}}), encoding="utf-8"
            )

            state = job_recovery.inspect_job(job_dir, planned=1)

        self.assertFalse(state.complete)
        self.assertEqual(state.running, 1)


class RecoveryMetadataTest(unittest.TestCase):
    def test_successful_recovery_records_invocations_and_recovered_count(self) -> None:
        initial = job_recovery.JobState(
            planned=4,
            completed=2,
            valid_completed=1,
            cancelled=1,
            pending=3,
            running=0,
            complete=False,
            incomplete_trials=("beta__two",),
        )
        final = job_recovery.JobState(
            planned=4,
            completed=4,
            valid_completed=4,
            cancelled=0,
            pending=0,
            running=0,
            complete=True,
            incomplete_trials=(),
        )

        payload = job_recovery.build_recovery_metadata(
            initial=initial,
            final=final,
            invocation_exit_codes=[1, 0],
        )

        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["initial_harbor_exit_code"], 1)
        self.assertEqual(payload["invocation_exit_codes"], [1, 0])
        self.assertEqual(payload["automatic_resume_count"], 1)
        self.assertEqual(payload["recovered_trials"], 3)
        self.assertEqual(payload["remaining_incomplete_trials"], 0)

    def test_incomplete_metadata_bounds_trial_samples(self) -> None:
        names = tuple(f"task__{index}" for index in range(30))
        state = job_recovery.JobState(
            planned=40,
            completed=10,
            valid_completed=10,
            cancelled=0,
            pending=30,
            running=0,
            complete=False,
            incomplete_trials=names,
        )

        payload = job_recovery.build_recovery_metadata(
            initial=state,
            final=state,
            invocation_exit_codes=[1],
        )

        self.assertEqual(payload["status"], "incomplete")
        self.assertEqual(len(payload["incomplete_trial_sample"]), 20)
        self.assertEqual(payload["remaining_incomplete_trials"], 30)


class RunShellContractTest(unittest.TestCase):
    def test_runner_preflights_cache_and_resumes_at_most_twice(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[1]
        runner = (root / "run.sh").read_text(encoding="utf-8")
        self.assertIn("task_cache.py\" preflight", runner)
        self.assertIn('MAX_AUTO_RESUMES=2', runner)
        self.assertIn('harbor jobs resume --job-path "$RUN_DIR"', runner)
        self.assertIn("job_recovery.py\" report", runner)
        self.assertIn('exit "$FINAL_RUN_RC"', runner)

    def test_recovery_cli_returns_nonzero_for_incomplete_job(self) -> None:
        root = pathlib.Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as tmp:
            temp = pathlib.Path(tmp)
            job_dir = temp / "job"
            job_dir.mkdir()
            initial = job_recovery.inspect_job(job_dir, planned=1)
            initial_path = temp / "initial.json"
            initial_path.write_text(
                json.dumps(
                    {
                        **initial.__dict__,
                        "incomplete_trials": list(initial.incomplete_trials),
                    }
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(root / "job_recovery.py"),
                    "report",
                    "--job-dir",
                    str(job_dir),
                    "--planned",
                    "1",
                    "--initial-state",
                    str(initial_path),
                    "--exit-codes",
                    "1,1",
                    "--output",
                    str(temp / "report.json"),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 1)

    def test_recovery_cli_returns_zero_for_exact_completed_job(self) -> None:
        root = pathlib.Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as tmp:
            temp = pathlib.Path(tmp)
            job_dir = temp / "job"
            job_dir.mkdir()
            write_trial(job_dir, "alpha__one")
            write_trial(job_dir, "beta__two")
            initial_path = temp / "initial.json"
            initial_path.write_text(
                json.dumps(
                    {
                        "planned": 2,
                        "completed": 1,
                        "valid_completed": 1,
                        "cancelled": 0,
                        "pending": 1,
                        "running": 0,
                        "complete": False,
                        "incomplete_trials": [],
                    }
                ),
                encoding="utf-8",
            )
            report_path = temp / "report.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(root / "job_recovery.py"),
                    "report",
                    "--job-dir",
                    str(job_dir),
                    "--planned",
                    "2",
                    "--initial-state",
                    str(initial_path),
                    "--exit-codes",
                    "1,0",
                    "--output",
                    str(report_path),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["recovered_trials"], 1)
        self.assertEqual(report["invocation_exit_codes"], [1, 0])


if __name__ == "__main__":
    unittest.main()
