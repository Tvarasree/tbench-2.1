#!/usr/bin/env python3
"""Contract tests for standardized Terminal-Bench result aggregation."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import results  # noqa: E402


def make_reward(run_dir: pathlib.Path, trial: str, reward: object) -> None:
    verifier = run_dir / trial / "verifier"
    verifier.mkdir(parents=True)
    if reward is not None:
        (verifier / "reward.txt").write_text(str(reward), encoding="utf-8")


class BuildResultsTest(unittest.TestCase):
    def test_preserves_main_and_adds_official_trial_accuracy(self) -> None:
        """Catches using solved tasks instead of successful trials for accuracy."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            for index, reward in enumerate((1, 1, 1, 0, 0), 1):
                make_reward(run_dir, f"alpha__{index}", reward)
            for index, reward in enumerate((1, 0, 0, 0, None), 1):
                make_reward(run_dir, f"beta__{index}", reward)
            for index in range(1, 6):
                make_reward(run_dir, f"gamma__{index}", 0)

            output = results.build_results(
                run_dir=run_dir,
                token_usage_path=run_dir / "missing-token-usage.json",
                agent="xyne-cli",
                model="private-large",
                dataset="terminal-bench/terminal-bench-2-1",
                attempts=5,
                selected_tasks=3,
                harbor_exit_code=0,
            )

        metrics = output["metrics"]
        self.assertEqual(metrics["main"], {"name": "Solved", "value": 2})
        self.assertEqual(metrics["secondary"]["solved"], 2)
        self.assertEqual(metrics["secondary"]["total"], 3)
        self.assertEqual(metrics["secondary"]["solve_rate_pct"], 66.67)
        self.assertEqual(metrics["secondary"]["successful_trials"], 4)
        self.assertEqual(metrics["secondary"]["planned_trials"], 15)
        self.assertEqual(metrics["secondary"]["trial_accuracy_pct"], 26.67)

    def test_solved_task_trial_success_is_equal_weight_average(self) -> None:
        """Catches dividing by observed trials or including unsolved tasks."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            for index, reward in enumerate((1, 1, 1, 0, 0), 1):
                make_reward(run_dir, f"alpha__{index}", reward)
            for index, reward in enumerate((1, 0, 0, 0, None), 1):
                make_reward(run_dir, f"beta__{index}", reward)
            for index in range(1, 6):
                make_reward(run_dir, f"gamma__{index}", 0)

            additional = results.build_results(
                run_dir=run_dir,
                token_usage_path=run_dir / "missing.json",
                agent="xyne-cli",
                model="private-large",
                dataset="terminal-bench/terminal-bench-2-1",
                attempts=5,
                selected_tasks=3,
                harbor_exit_code=0,
            )["metrics"]["additional"]

        repeatability = additional["solved_task_trial_success"]
        self.assertEqual(additional["solved_task_trial_success_pct"], 40.0)
        self.assertEqual(repeatability["solved_tasks"], 2)
        self.assertEqual(repeatability["successful_trials"], 4)
        self.assertEqual(repeatability["planned_trials"], 10)
        self.assertEqual(repeatability["per_task_pct"], {"alpha": 60.0, "beta": 20.0})

    def test_repeatability_rounds_only_after_averaging_exact_task_rates(self) -> None:
        """Catches cumulative error from averaging display-rounded task rates."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            for index, reward in enumerate((1, 0, 0), 1):
                make_reward(run_dir, f"alpha__{index}", reward)
            for index in range(1, 4):
                make_reward(run_dir, f"beta__{index}", 1)

            additional = results.build_results(
                run_dir=run_dir,
                token_usage_path=run_dir / "missing.json",
                agent="xyne-cli",
                model="private-large",
                dataset="terminal-bench/terminal-bench-2-1",
                attempts=3,
                selected_tasks=2,
                harbor_exit_code=0,
            )["metrics"]["additional"]

        self.assertEqual(additional["solved_task_trial_success_pct"], 66.67)
        self.assertEqual(
            additional["solved_task_trial_success"]["per_task_pct"],
            {"alpha": 33.33, "beta": 100.0},
        )

    def test_missing_trials_are_failures_in_planned_denominators(self) -> None:
        """Catches silently shrinking official denominators to artifact count."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_reward(run_dir, "alpha__1", 1)
            make_reward(run_dir, "alpha__2", 0)

            metrics = results.build_results(
                run_dir=run_dir,
                token_usage_path=run_dir / "missing.json",
                agent="xyne-cli",
                model="private-large",
                dataset="terminal-bench/terminal-bench-2-1",
                attempts=3,
                selected_tasks=2,
                harbor_exit_code=1,
            )["metrics"]

        self.assertEqual(metrics["secondary"]["successful_trials"], 1)
        self.assertEqual(metrics["secondary"]["planned_trials"], 6)
        self.assertEqual(metrics["secondary"]["trial_accuracy_pct"], 16.67)
        self.assertEqual(metrics["additional"]["solved_task_trial_success_pct"], 33.33)

    def test_no_solved_tasks_has_no_repeatability_population(self) -> None:
        """Catches presenting an empty solved-task population as 0% reliability."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_reward(run_dir, "alpha__1", 0)
            metrics = results.build_results(
                run_dir=run_dir,
                token_usage_path=run_dir / "missing.json",
                agent="xyne-cli",
                model="private-large",
                dataset="terminal-bench/terminal-bench-2-1",
                attempts=1,
                selected_tasks=1,
                harbor_exit_code=0,
            )["metrics"]

        self.assertIsNone(metrics["additional"]["solved_task_trial_success_pct"])

    def test_hidden_harbor_directories_are_not_counted_as_tasks(self) -> None:
        """Catches treating Harbor's .sources cache as a no-grade task."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_reward(run_dir, "alpha__1", 1)
            (run_dir / ".sources" / "cached-trial").mkdir(parents=True)
            metrics = results.build_results(
                run_dir=run_dir,
                token_usage_path=run_dir / "missing.json",
                agent="xyne-cli",
                model="private-large",
                dataset="terminal-bench/terminal-bench-2-1",
                attempts=1,
                selected_tasks=1,
                harbor_exit_code=0,
            )["metrics"]

        self.assertEqual(metrics["secondary"]["no_grade"], 0)
        self.assertEqual(list(metrics["additional"]["per_task"]), ["alpha"])

    def test_token_headline_is_defensive_and_preserves_new_fields(self) -> None:
        """Catches dropping successful-trial token metrics at the dashboard boundary."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            make_reward(root / "run", "alpha__1", 1)
            token_path = root / "token_usage.json"
            token_path.write_text(
                json.dumps(
                    {
                        "meta": {"priced": False, "agent_versions": ["0.3.3"]},
                        "aggregates": {
                            "coverage": {
                                "measured_attempts": 1,
                                "priceable_attempts": 0,
                                "total_attempts": 1,
                                "measured_pct": 100.0,
                                "quality_counts": {"full": 1},
                                "note": "All attempts reported token usage.",
                            },
                            "totals": {
                                "n_input_tokens": 80,
                                "n_cache_tokens": 0,
                                "n_output_tokens": 20,
                                "n_total_tokens": 100,
                                "cost_usd_billed": None,
                                "cost_usd_priced": None,
                            },
                            "cost_per_success": {
                                "cost_usd_per_solve_including_failed_retries": None,
                                "cost_usd_per_solve_winning_attempt_only": None,
                                "successful_trials": 1,
                                "measured_successful_trials": 1,
                                "tokens_successful_trials": 100,
                                "avg_tokens_per_successful_trial": 100.0,
                                "priceable_successful_trials": 0,
                                "cost_usd_successful_trials": None,
                                "avg_cost_usd_per_successful_trial": None,
                            },
                            "waste": {
                                "cost_usd_wasted": None,
                                "wasted_pct": None,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            headline = results.build_results(
                run_dir=root / "run",
                token_usage_path=token_path,
                agent="xyne-cli",
                model="private-large",
                dataset="terminal-bench/terminal-bench-2-1",
                attempts=1,
                selected_tasks=1,
                harbor_exit_code=0,
            )["metrics"]["additional"]["token_usage"]

        self.assertEqual(headline["successful_trials"], 1)
        self.assertEqual(headline["measured_successful_trials"], 1)
        self.assertEqual(headline["avg_tokens_per_successful_trial"], 100.0)


if __name__ == "__main__":
    unittest.main()
