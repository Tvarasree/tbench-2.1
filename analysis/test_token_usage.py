#!/usr/bin/env python3
"""Tests for the token-usage reporter and the xyne session-usage parser.

Pure stdlib, no harbor and no network — runnable anywhere:

    python3 -m unittest discover -s analysis -p 'test_*.py' -v

The fixtures are synthetic harbor trial trees. Numbers are chosen so every
assertion can be re-derived by hand from the docstring of the test, which is
the only way an aggregation bug gets caught before a real run.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "adapter"))

import token_usage as tu  # noqa: E402
from xyne_harbor_agent.session_usage import (  # noqa: E402
    sum_session_usage,
    to_harbor_fields,
)


def make_trial(
    run_dir: pathlib.Path,
    name: str,
    *,
    reward: float | None,
    tokens: tuple[int, int, int] | None,
    cost: float | None = None,
    multi_step: bool = False,
    agent_version: str = "0.3.3",
) -> None:
    """Write one synthetic trial dir. tokens=(input_incl_cache, cache, output)."""
    trial = run_dir / name
    trial.mkdir(parents=True)

    if reward is not None:
        (trial / "verifier").mkdir()
        (trial / "verifier" / "reward.txt").write_text(f"{reward}\n")

    if tokens is None:
        context = {
            "n_input_tokens": None,
            "n_cache_tokens": None,
            "n_output_tokens": None,
            "cost_usd": None,
        }
    else:
        n_in, n_cache, n_out = tokens
        context = {
            "n_input_tokens": n_in,
            "n_cache_tokens": n_cache,
            "n_output_tokens": n_out,
            "cost_usd": cost,
        }

    if multi_step:
        # Multi-step trials leave agent_result null and split the context
        # across step_results; harbor's own aggregator handles both shapes.
        step = {
            "n_input_tokens": context["n_input_tokens"] // 2,
            "n_cache_tokens": context["n_cache_tokens"] // 2,
            "n_output_tokens": context["n_output_tokens"] // 2,
            "cost_usd": (cost / 2) if cost else None,
        }
        result = {
            "agent_result": None,
            "step_results": [
                {"step_name": "a", "agent_result": step},
                {"step_name": "b", "agent_result": step},
            ],
        }
    else:
        result = {"agent_result": context, "step_results": None}

    result["agent_info"] = {"name": "xyne-cli", "version": agent_version}
    (trial / "result.json").write_text(json.dumps(result))


class LoadAttemptsTest(unittest.TestCase):
    def test_outcome_and_task_grouping(self) -> None:
        """reward>=1 solved, <1 unsolved, missing reward.txt no-grade."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_trial(run_dir, "alpha__1", reward=1.0, tokens=(100, 0, 10))
            make_trial(run_dir, "alpha__2", reward=0.0, tokens=(200, 0, 20))
            make_trial(run_dir, "beta__1", reward=None, tokens=(300, 0, 30))

            attempts = tu.load_attempts(run_dir)
            by_trial = {a["trial"]: a for a in attempts}

            self.assertEqual(by_trial["alpha__1"]["outcome"], "solved")
            self.assertEqual(by_trial["alpha__2"]["outcome"], "unsolved")
            self.assertEqual(by_trial["beta__1"]["outcome"], "no-grade")
            self.assertEqual(by_trial["alpha__1"]["task"], "alpha")
            self.assertEqual(by_trial["beta__1"]["task"], "beta")
            # Attempt index is positional within a task, by sorted trial name.
            self.assertEqual(by_trial["alpha__1"]["attempt_index"], 1)
            self.assertEqual(by_trial["alpha__2"]["attempt_index"], 2)
            self.assertEqual(by_trial["beta__1"]["attempt_index"], 1)

    def test_unmeasured_when_no_token_fields(self) -> None:
        """An all-null context is unmeasured, not zero usage."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_trial(run_dir, "aider-task__1", reward=1.0, tokens=None)
            attempt = tu.load_attempts(run_dir)[0]
            self.assertFalse(attempt["measured"])
            self.assertEqual(attempt["n_total_tokens"], 0)

    def test_hidden_harbor_directories_are_not_attempts(self) -> None:
        """Harbor caches such as .sources must not reduce usage coverage."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_trial(run_dir, "alpha__1", reward=1.0, tokens=(80, 0, 20))
            (run_dir / ".sources" / "cached-task").mkdir(parents=True)

            attempts = tu.load_attempts(run_dir)

        self.assertEqual([attempt["trial"] for attempt in attempts], ["alpha__1"])

    def test_multi_step_contexts_are_summed(self) -> None:
        """step_results are aggregated when agent_result is null."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_trial(
                run_dir, "t__1", reward=1.0, tokens=(1000, 400, 60),
                cost=0.5, multi_step=True,
            )
            attempt = tu.load_attempts(run_dir)[0]
            self.assertTrue(attempt["measured"])
            self.assertEqual(attempt["n_input_tokens"], 1000)   # 500 + 500
            self.assertEqual(attempt["n_cache_tokens"], 400)    # 200 + 200
            self.assertEqual(attempt["n_output_tokens"], 60)    # 30 + 30
            self.assertAlmostEqual(attempt["cost_usd_billed"], 0.5)

    def test_missing_run_dir_is_empty(self) -> None:
        self.assertEqual(tu.load_attempts(pathlib.Path("/nonexistent/xyz")), [])

    def test_agent_version_is_captured_from_agent_info(self) -> None:
        """The only per-run record of which unpinned xyne build actually ran."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_trial(run_dir, "a__1", reward=1.0, tokens=(1, 0, 1),
                       agent_version="0.3.3")
            self.assertEqual(tu.load_attempts(run_dir)[0]["agent_version"], "0.3.3")

    def test_missing_agent_info_degrades_to_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            trial = run_dir / "a__1"
            (trial / "verifier").mkdir(parents=True)
            (trial / "verifier" / "reward.txt").write_text("1.0\n")
            (trial / "result.json").write_text(json.dumps({"agent_result": None}))
            self.assertEqual(tu.load_attempts(run_dir)[0]["agent_version"], "")

    def test_xyne_raw_sessions_fill_empty_harbor_context(self) -> None:
        """The final reporter recovers usage when Harbor's hook ran too early."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_trial(run_dir, "xyne-task__1", reward=1.0, tokens=None)
            sessions = run_dir / "xyne-task__1" / "agent" / "sessions" / "-app"
            sessions.mkdir(parents=True)
            (sessions / "session.jsonl").write_text(
                json.dumps({
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "usage": {
                            "input": 100,
                            "output": 20,
                            "cacheRead": 300,
                            "cacheWrite": 40,
                            "cost": {"total": 0},
                        },
                    },
                }) + "\n"
            )

            attempt = tu.load_attempts(run_dir, agent="xyne-cli")[0]

            self.assertTrue(attempt["measured"])
            self.assertEqual(attempt["usage_source"], "xyne-session-jsonl")
            self.assertEqual(attempt["n_input_tokens"], 440)
            self.assertEqual(attempt["n_cache_read_tokens"], 300)
            self.assertEqual(attempt["n_cache_write_tokens"], 40)
            self.assertEqual(attempt["n_output_tokens"], 20)

    def test_supported_agent_raw_usage_overrides_lossy_harbor_context(self) -> None:
        """Raw parsers preserve cache writes that Harbor 0.13.1 drops."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_trial(run_dir, "opencode-task__1", reward=0.0,
                       tokens=(400, 300, 20))
            agent_dir = run_dir / "opencode-task__1" / "agent"
            agent_dir.mkdir()
            (agent_dir / "opencode.txt").write_text(json.dumps({
                "type": "step_finish",
                "part": {
                    "tokens": {
                        "input": 100,
                        "output": 20,
                        "cache": {"read": 300, "write": 40},
                    },
                    "cost": 0,
                },
            }) + "\n")

            attempt = tu.load_attempts(run_dir, agent="opencode")[0]

            self.assertEqual(attempt["usage_source"], "opencode-step-finish-jsonl")
            self.assertEqual(attempt["n_input_tokens"], 440)
            self.assertEqual(attempt["n_cache_read_tokens"], 300)
            self.assertEqual(attempt["n_cache_write_tokens"], 40)

    def test_goose_total_only_fallback_is_counted_but_not_priceable(self) -> None:
        """Harbor 0.13.1 stores Goose total_tokens in its input field."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            trial = run_dir / "goose-task__1"
            trial.mkdir(parents=True)
            (trial / "result.json").write_text(json.dumps({
                "agent_info": {"name": "goose", "version": "1"},
                "agent_result": {
                    "n_input_tokens": 1234,
                    "n_cache_tokens": None,
                    "n_output_tokens": None,
                    "cost_usd": None,
                },
            }))

            attempt = tu.load_attempts(run_dir, agent="goose")[0]
            aggregate = tu.aggregate([attempt], tu.Pricing(3.0, 15.0))

            self.assertTrue(attempt["measured"])
            self.assertEqual(attempt["measurement_quality"], "total_only")
            self.assertFalse(attempt["priceable"])
            self.assertEqual(attempt["n_input_tokens"], 0)
            self.assertEqual(attempt["n_total_tokens"], 1234)
            self.assertIsNone(attempt["cost_usd_priced"])
            self.assertEqual(aggregate["coverage"]["priceable_attempts"], 0)
            self.assertIsNone(aggregate["totals"]["cost_usd_priced"])
            self.assertIsNone(
                aggregate["cost_per_success"][
                    "cost_usd_per_solve_including_failed_retries"
                ]
            )
            self.assertEqual(
                aggregate["coverage"]["quality_counts"]["total_only"], 1
            )
            report = {
                "meta": {
                    "eval_run_id": "goose-total-only",
                    "agent": "goose",
                    "model": "model",
                    "attempts": 1,
                    "generated_at": "now",
                    "pricing_note": "test prices",
                },
                "aggregates": aggregate,
                "attempts": [attempt],
            }
            html = pathlib.Path(tmp) / "token_usage.html"
            tu.write_html(html, report)
            self.assertIn("&mdash;", html.read_text())


class AgentVersionReportingTest(unittest.TestCase):
    def test_mixed_versions_are_surfaced_not_averaged(self) -> None:
        """An always-latest install can change build mid-run; say so loudly."""
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            run_dir = out / "run"
            run_dir.mkdir()
            make_trial(run_dir, "a__1", reward=1.0, tokens=(10, 0, 1),
                       agent_version="0.3.3")
            make_trial(run_dir, "b__1", reward=0.0, tokens=(10, 0, 1),
                       agent_version="0.3.4")
            tu.main(["--run-dir", str(run_dir), "--out-dir", str(out)])
            report = json.loads((out / "token_usage.json").read_text())
            self.assertEqual(report["meta"]["agent_versions"], ["0.3.3", "0.3.4"])
            md = (out / "token_usage.md").read_text()
            self.assertIn("2 distinct agent versions", md)

    def test_single_version_reported_plainly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            run_dir = out / "run"
            run_dir.mkdir()
            make_trial(run_dir, "a__1", reward=1.0, tokens=(10, 0, 1),
                       agent_version="0.3.3")
            tu.main(["--run-dir", str(run_dir), "--out-dir", str(out)])
            md = (out / "token_usage.md").read_text()
            self.assertIn("version: 0.3.3", md)
            self.assertNotIn("distinct agent versions", md)


class PricingTest(unittest.TestCase):
    def test_requires_both_input_and_output(self) -> None:
        """A partial price list is never half-applied."""
        self.assertFalse(tu.Pricing(3.0, None).enabled)
        self.assertFalse(tu.Pricing(None, 15.0).enabled)
        self.assertIn("NOT PRICED", tu.Pricing(3.0, None).note)
        self.assertTrue(tu.Pricing(3.0, 15.0).enabled)

    def test_all_input_uses_the_input_rate(self) -> None:
        """1000 total input * 3 + 100 output * 15 = 4500 / 1M."""
        pricing = tu.Pricing(3.0, 15.0)
        self.assertAlmostEqual(pricing.cost(1000, 100), 0.0045)

    def test_unpriced_returns_none(self) -> None:
        self.assertIsNone(tu.Pricing(None, None).cost(1000, 100))


class AggregateTest(unittest.TestCase):
    """A 2-task run, 2 attempts each, priced at 1.0 in / 1.0 out per 1M.

    At those rates cost == tokens / 1e6, so every figure is checkable by eye.

      alpha__1  unsolved  100 in / 0 cache / 100 out  -> 200 tok
      alpha__2  solved    100 in / 0 cache / 100 out  -> 200 tok
      beta__1   unsolved  300 in / 0 cache / 100 out  -> 400 tok
      beta__2   unsolved  300 in / 0 cache / 100 out  -> 400 tok

    total 1200 tok; solved bucket 200; unsolved bucket 1000.
    One task solved, bought by alpha__2 at 200 tok.
    """

    def _run(self) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_trial(run_dir, "alpha__1", reward=0.0, tokens=(100, 0, 100))
            make_trial(run_dir, "alpha__2", reward=1.0, tokens=(100, 0, 100))
            make_trial(run_dir, "beta__1", reward=0.0, tokens=(300, 0, 100))
            make_trial(run_dir, "beta__2", reward=0.0, tokens=(300, 0, 100))
            attempts = tu.load_attempts(run_dir)
            return tu.aggregate(attempts, tu.Pricing(1.0, 1.0))

    def test_totals_and_coverage(self) -> None:
        agg = self._run()
        self.assertEqual(agg["totals"]["n_total_tokens"], 1200)
        self.assertAlmostEqual(agg["totals"]["cost_usd_priced"], 0.0012)
        self.assertTrue(agg["coverage"]["complete"])
        self.assertEqual(agg["coverage"]["measured_pct"], 100.0)

    def test_outcome_split_uses_per_attempt_averages(self) -> None:
        """The comparison that matters is avg/attempt, not the bucket total."""
        agg = self._run()
        solved = agg["by_outcome"]["solved"]
        unsolved = agg["by_outcome"]["unsolved"]
        self.assertEqual(solved["attempts"], 1)
        self.assertEqual(unsolved["attempts"], 3)
        self.assertEqual(solved["avg_total_tokens_per_attempt"], 200.0)
        # 1000 tokens over 3 failing attempts.
        self.assertAlmostEqual(
            unsolved["avg_total_tokens_per_attempt"], 333.3, places=1
        )

    def test_cost_per_success_both_ways(self) -> None:
        agg = self._run()
        cps = agg["cost_per_success"]
        self.assertEqual(cps["tasks_solved"], 1)
        # Winning attempt only: 200 tokens.
        self.assertEqual(cps["tokens_per_solve_winning_attempt_only"], 200.0)
        # Honest figure: the whole run bought one solve.
        self.assertEqual(cps["tokens_per_solve_including_failed_retries"], 1200.0)

    def test_successful_trial_average_excludes_failed_trials(self) -> None:
        """Catches treating isolated failed trials as inputs to a successful trial."""
        agg = self._run()
        success = agg["cost_per_success"]
        self.assertEqual(success["successful_trials"], 1)
        self.assertEqual(success["measured_successful_trials"], 1)
        self.assertEqual(success["tokens_successful_trials"], 200)
        self.assertEqual(success["avg_tokens_per_successful_trial"], 200.0)
        self.assertEqual(success["priceable_successful_trials"], 1)
        self.assertAlmostEqual(success["cost_usd_successful_trials"], 0.0002)
        self.assertAlmostEqual(
            success["avg_cost_usd_per_successful_trial"], 0.0002
        )

    def test_successful_trial_average_accepts_fractional_positive_reward(self) -> None:
        """Official trial success is reward > 0, independently of task solved >= 1."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_trial(run_dir, "a__1", reward=0.5, tokens=(80, 0, 20))
            success = tu.aggregate(
                tu.load_attempts(run_dir), tu.Pricing(1.0, 1.0)
            )["cost_per_success"]

        self.assertEqual(success["tasks_solved"], 0)
        self.assertEqual(success["successful_trials"], 1)
        self.assertEqual(success["measured_successful_trials"], 1)
        self.assertEqual(success["tokens_successful_trials"], 100)
        self.assertEqual(success["avg_tokens_per_successful_trial"], 100.0)

    def test_successful_trial_average_uses_only_measured_successes(self) -> None:
        """Catches treating missing successful-trial telemetry as zero tokens."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_trial(run_dir, "a__1", reward=1.0, tokens=(80, 0, 20))
            make_trial(run_dir, "a__2", reward=1.0, tokens=(160, 0, 40))
            make_trial(run_dir, "b__1", reward=1.0, tokens=None)
            make_trial(run_dir, "b__2", reward=0.0, tokens=(400, 0, 100))
            success = tu.aggregate(
                tu.load_attempts(run_dir), tu.Pricing(1.0, 1.0)
            )["cost_per_success"]

        self.assertEqual(success["successful_trials"], 3)
        self.assertEqual(success["measured_successful_trials"], 2)
        self.assertEqual(success["tokens_successful_trials"], 300)
        self.assertEqual(success["avg_tokens_per_successful_trial"], 150.0)
        self.assertEqual(success["priceable_successful_trials"], 2)
        self.assertAlmostEqual(success["cost_usd_successful_trials"], 0.0003)
        self.assertAlmostEqual(
            success["avg_cost_usd_per_successful_trial"], 0.00015
        )

    def test_unpriced_successful_trial_cost_average_is_null(self) -> None:
        """Catches presenting zero dollars when no pricing was supplied."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_trial(run_dir, "a__1", reward=1.0, tokens=(80, 0, 20))
            success = tu.aggregate(
                tu.load_attempts(run_dir), tu.Pricing(None, None)
            )["cost_per_success"]

        self.assertEqual(success["avg_tokens_per_successful_trial"], 100.0)
        self.assertIsNone(success["cost_usd_successful_trials"])
        self.assertIsNone(success["avg_cost_usd_per_successful_trial"])

    def test_waste_is_everything_not_attributable_to_a_solve(self) -> None:
        agg = self._run()
        waste = agg["waste"]
        self.assertEqual(waste["tokens_attributable_to_a_solve"], 200)
        self.assertEqual(waste["tokens_wasted"], 1000)
        self.assertAlmostEqual(waste["tokens_wasted_pct"], 83.33, places=2)
        self.assertAlmostEqual(waste["cost_usd_wasted"], 0.001)
        self.assertAlmostEqual(waste["wasted_pct"], 83.33, places=2)

    def test_by_round_tracks_solves_and_cost_per_solve(self) -> None:
        agg = self._run()
        rounds = agg["by_round"]
        self.assertEqual(rounds["1"]["attempts"], 2)
        self.assertEqual(rounds["1"]["solves"], 0)
        self.assertIsNone(rounds["1"]["tokens_per_solve"])
        self.assertIsNone(rounds["1"]["cost_usd_per_solve"])
        self.assertEqual(rounds["2"]["solves"], 1)
        # Round 2 spent 200 + 400 = 600 tokens to buy one solve.
        self.assertEqual(rounds["2"]["tokens_per_solve"], 600.0)
        self.assertAlmostEqual(rounds["2"]["cost_usd_per_solve"], 0.0006)

    def test_zero_token_total_has_no_waste_percentage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_trial(run_dir, "zero__1", reward=0.0, tokens=(0, 0, 0))
            waste = tu.aggregate(
                tu.load_attempts(run_dir), tu.Pricing(None, None)
            )["waste"]

        self.assertEqual(waste["tokens_wasted"], 0)
        self.assertIsNone(waste["tokens_wasted_pct"])

    def test_first_solve_is_the_winning_attempt(self) -> None:
        """A later attempt on an already-solved task did not buy the solve."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_trial(run_dir, "t__1", reward=1.0, tokens=(100, 0, 0))
            make_trial(run_dir, "t__2", reward=1.0, tokens=(900, 0, 0))
            agg = tu.aggregate(
                tu.load_attempts(run_dir), tu.Pricing(1.0, 1.0)
            )
            self.assertEqual(
                agg["by_task"]["t"]["n_total_tokens_winning_attempt"], 100
            )
            self.assertEqual(agg["waste"]["tokens_wasted"], 900)

    def test_partial_coverage_is_flagged_as_lower_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_trial(run_dir, "a__1", reward=1.0, tokens=(100, 0, 100))
            make_trial(run_dir, "b__1", reward=0.0, tokens=None)
            agg = tu.aggregate(
                tu.load_attempts(run_dir), tu.Pricing(1.0, 1.0)
            )
            cov = agg["coverage"]
            self.assertFalse(cov["complete"])
            self.assertEqual(cov["measured_attempts"], 1)
            self.assertEqual(cov["total_attempts"], 2)
            self.assertIn("LOWER BOUND", cov["note"])

    def test_no_solves_leaves_cost_per_success_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = pathlib.Path(tmp)
            make_trial(run_dir, "a__1", reward=0.0, tokens=(100, 0, 100))
            agg = tu.aggregate(
                tu.load_attempts(run_dir), tu.Pricing(1.0, 1.0)
            )
            cps = agg["cost_per_success"]
            self.assertEqual(cps["tasks_solved"], 0)
            self.assertIsNone(cps["cost_usd_per_solve_including_failed_retries"])


class EmittersTest(unittest.TestCase):
    def _build(self, out_dir: pathlib.Path, priced: bool) -> int:
        run_dir = out_dir / "run"
        run_dir.mkdir()
        make_trial(
            run_dir,
            "alpha__1",
            reward=1.0,
            tokens=(100, 40, 100),
            cost=0.01 if priced else None,
        )
        make_trial(
            run_dir,
            "beta__1",
            reward=0.0,
            tokens=(300, 0, 100),
            cost=0.02 if priced else None,
        )
        argv = [
            "--run-dir", str(run_dir),
            "--out-dir", str(out_dir),
            "--eval-run-id", "test-run",
            "--agent", "xyne-cli",
            "--model", "private-large",
            "--attempts", "1",
        ]
        if priced:
            argv += [
                "--price-input", "3",
                "--price-output", "15",
            ]
        return tu.main(argv)

    def test_html_token_formatter_uses_two_decimal_compact_units(self) -> None:
        formatter = getattr(tu, "_fmt_html_tokens", None)
        self.assertTrue(callable(formatter))
        self.assertEqual(formatter(999), "999")
        self.assertEqual(formatter(600.0), "600")
        self.assertEqual(formatter(1_000), "1K")
        self.assertEqual(formatter(12_350), "12.35K")
        self.assertEqual(formatter(35_000), "35K")
        self.assertEqual(formatter(999_999), "1M")
        self.assertEqual(formatter(1_450_000), "1.45M")

    def test_html_compacts_token_values_without_changing_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            run_dir = out / "run"
            run_dir.mkdir()
            make_trial(
                run_dir,
                "large__1",
                reward=1.0,
                tokens=(1_450_000, 0, 0),
            )

            self.assertEqual(
                tu.main(
                    [
                        "--run-dir", str(run_dir),
                        "--out-dir", str(out),
                        "--eval-run-id", "large-run",
                    ]
                ),
                0,
            )
            html = (out / "token_usage.html").read_text(encoding="utf-8")
            report = json.loads((out / "token_usage.json").read_text())

        self.assertIn("1.45M tokens", html)
        self.assertNotIn("1,450,000 tokens", html)
        self.assertEqual(report["aggregates"]["totals"]["n_total_tokens"], 1_450_000)

    def test_unpriced_run_emits_all_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            self.assertEqual(self._build(out, priced=False), 0)
            for name in (
                "token_usage.json",
                "token_usage.csv",
                "token_usage.md",
                "token_usage.html",
            ):
                self.assertTrue((out / name).is_file(), name)
            html = (out / "token_usage.html").read_text(encoding="utf-8")
            self.assertIn("Terminal-Bench Token Usage", html)
            self.assertIn("Token-only report", html)
            self.assertIn("Tokens per solved task, including all trials", html)
            self.assertIn("600 tokens per solved task", html)
            self.assertIn("Including failed retries", html)
            self.assertIn("Winning attempt only", html)
            self.assertIn("Average successful trial", html)
            self.assertIn("Tokens/solve", html)
            self.assertIn("66.67% of measured tokens", html)
            self.assertNotIn("Custom-priced cost", html)
            self.assertNotIn("Avg cost/trial", html)
            self.assertNotIn("Cost/solve", html)
            report = json.loads((out / "token_usage.json").read_text())
            self.assertFalse(report["meta"]["priced"])
            self.assertIn("NOT PRICED", report["meta"]["pricing_note"])
            self.assertIsNone(
                report["aggregates"]["totals"]["cost_usd_priced"]
            )
            self.assertIsNone(
                report["aggregates"]["totals"]["cost_usd_billed"]
            )
            markdown = (out / "token_usage.md").read_text()
            self.assertIn("Average successful trial", markdown)
            self.assertIn("1/1 successful trials measured", markdown)

    def test_upstream_cost_does_not_drive_unpriced_charts(self) -> None:
        def render(root: pathlib.Path, billed: bool) -> str:
            run_dir = root / "run"
            run_dir.mkdir()
            make_trial(
                run_dir,
                "alpha__1",
                reward=1.0,
                tokens=(100, 0, 100),
                cost=9.0 if billed else None,
            )
            make_trial(
                run_dir,
                "beta__1",
                reward=0.0,
                tokens=(300, 0, 100),
                cost=17.0 if billed else None,
            )
            report = tu.build_report(
                tu.load_attempts(run_dir),
                tu.Pricing(None, None),
                {
                    "eval_run_id": "billed" if billed else "no-billed",
                    "agent": "xyne-cli",
                    "model": "private-large",
                    "attempts": "1",
                    "generated_at": "2026-08-04T00:00:00Z",
                    "pricing_note": "NOT PRICED",
                    "priced": False,
                    "agent_versions": [],
                },
            )
            output = root / "token_usage.html"
            tu.write_html(output, report)
            return output.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as billed_tmp:
            billed_html = render(pathlib.Path(billed_tmp), billed=True)
        with tempfile.TemporaryDirectory() as plain_tmp:
            plain_html = render(pathlib.Path(plain_tmp), billed=False)

        billed_widths = re.findall(
            r'data-chart="success" style="width:([0-9.]+)%', billed_html
        )
        plain_widths = re.findall(
            r'data-chart="success" style="width:([0-9.]+)%', plain_html
        )
        self.assertEqual(billed_widths, plain_widths)
        self.assertTrue(billed_widths)
        self.assertIn("Upstream billed cost (secondary telemetry)", billed_html)
        self.assertNotIn("Upstream billed cost (secondary telemetry)", plain_html)

    def test_partial_pricing_emits_token_only_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            run_dir = out / "run"
            run_dir.mkdir()
            make_trial(run_dir, "alpha__1", reward=1.0, tokens=(100, 0, 100))

            code = tu.main(
                [
                    "--run-dir", str(run_dir),
                    "--out-dir", str(out),
                    "--price-input", "3",
                ]
            )

            self.assertEqual(code, 0)
            self.assertTrue((out / "token_usage.html").is_file())
            report = json.loads((out / "token_usage.json").read_text())
            self.assertFalse(report["meta"]["priced"])
            self.assertIn("NOT PRICED", report["meta"]["pricing_note"])
            html = (out / "token_usage.html").read_text(encoding="utf-8")
            self.assertIn("Token-only report", html)
            self.assertNotIn("Custom-priced cost", html)

    def test_priced_run_emits_html_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            self.assertEqual(self._build(out, priced=True), 0)
            self.assertTrue((out / "token_usage.html").is_file())
            html = (out / "token_usage.html").read_text()
            self.assertIn('class="stack"', html)
            self.assertIn('class="bars"', html)
            self.assertIn("Including failed retries", html)
            self.assertIn("Winning attempt only", html)
            self.assertIn("Average successful trial", html)
            self.assertIn("1/1 successful trials measured", html)
            self.assertNotIn("Cache read", html)
            self.assertNotIn("Cache write", html)
            self.assertIn(
                '$0.0042<span class="token-value">600 tokens</span>', html
            )
            for heading in (
                "Priceable",
                "Avg tokens/trial",
                "Avg cost/trial",
                "Billed cost",
                "Successful-trial totals",
                "Coverage quality",
                "Winning tokens",
                "Winning cost",
                "Attributable to solves",
                "Not attributable to solves",
            ):
                self.assertIn(heading, html)
            beta_row = html.split("<td>beta</td>", 1)[1].split("</tr>", 1)[0]
            self.assertNotIn("$0.0000", beta_row)
            self.assertTrue(beta_row.endswith('<td class="n">&mdash;</td>'))
            solved_row = html.split("</span>Solved</span></td>", 1)[1].split(
                "</tr>", 1
            )[0]
            self.assertEqual(
                solved_row,
                '<td class="n">1</td><td class="n">1</td>'
                '<td class="n">1</td><td class="n">100</td>'
                '<td class="n">100</td><td class="n">200</td>'
                '<td class="n">200</td><td class="n">$0.0018</td>'
                '<td class="n">$0.0018</td><td class="n">$0.0100</td>',
            )
            round_row = html.split("<tr><td>Attempt 1</td>", 1)[1].split(
                "</tr>", 1
            )[0]
            self.assertEqual(
                round_row,
                '<td class="n">2</td><td class="n">1</td>'
                '<td class="n">2</td><td class="n">2</td>'
                '<td class="n">400</td><td class="n">200</td>'
                '<td class="n">600</td><td class="n">300</td>'
                '<td class="n">$0.0042</td><td class="n">$0.0021</td>'
                '<td class="n">$0.0300</td><td class="n">$0.0042</td>',
            )
            self.assertIn("Billed upstream: $0.0300", html)
            self.assertIn(
                "Successful-trial totals: 200 measured tokens and\n"
                "$0.0018 across 1 successful trials.",
                html,
            )
            self.assertIn(
                "Coverage quality:</strong> full 2 &middot; partial 0 &middot; "
                "total only 0 &middot; unmeasured 0.",
                html,
            )
            self.assertIn("$0.0018</div>\n<div class=\"foot\">200 tokens", html)
            self.assertIn("$0.0024</div>\n<div class=\"foot\">400 tokens", html)
            report = json.loads((out / "token_usage.json").read_text())
            self.assertTrue(report["meta"]["priced"])
            # alpha: 100 input*3 + 100 output*15 = 1800
            # beta:  300 input*3 + 100 output*15 = 2400
            # total 4200 / 1e6
            self.assertAlmostEqual(
                report["aggregates"]["totals"]["cost_usd_priced"], 0.0042
            )

    def test_html_escapes_artifact_labels_and_handles_zero_bars(self) -> None:
        """Catches HTML injection and division by zero in visual normalization."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            run_dir = root / "run"
            run_dir.mkdir()
            make_trial(
                run_dir,
                "<script>alert(1)<script>__1",
                reward=1.0,
                tokens=(0, 0, 0),
            )
            attempts = tu.load_attempts(run_dir)
            report = tu.build_report(
                attempts,
                tu.Pricing(1.0, 1.0),
                {
                    "eval_run_id": "<unsafe-run>",
                    "agent": "<unsafe-agent>",
                    "model": "<unsafe-model>",
                    "attempts": "1",
                    "generated_at": "2026-08-03T00:00:00Z",
                    "pricing_note": "<unsafe-price-note>",
                    "priced": True,
                    "agent_versions": ["<unsafe-version>"],
                },
            )
            report["aggregates"]["coverage"]["note"] = "<unsafe-coverage-note>"
            output = root / "token_usage.html"

            tu.write_html(output, report)
            html = output.read_text(encoding="utf-8")

        self.assertNotIn("<script>alert(1)<script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;script&gt;", html)
        for sentinel in (
            "unsafe-run",
            "unsafe-agent",
            "unsafe-model",
            "unsafe-price-note",
            "unsafe-version",
            "unsafe-coverage-note",
        ):
            self.assertNotIn(f"<{sentinel}>", html)
            self.assertIn(f"&lt;{sentinel}&gt;", html)
        for chart in ("success", "outcome", "round"):
            self.assertIn(
                f'data-chart="{chart}" style="width:0.00%',
                html,
            )

    def test_unpriced_html_handles_zero_bars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            run_dir = root / "run"
            run_dir.mkdir()
            make_trial(run_dir, "zero__1", reward=1.0, tokens=(0, 0, 0))
            report = tu.build_report(
                tu.load_attempts(run_dir),
                tu.Pricing(None, None),
                {
                    "eval_run_id": "zero-run",
                    "agent": "xyne-cli",
                    "model": "private-large",
                    "attempts": "1",
                    "generated_at": "2026-08-04T00:00:00Z",
                    "pricing_note": "NOT PRICED",
                    "priced": False,
                    "agent_versions": [],
                },
            )
            output = root / "token_usage.html"
            tu.write_html(output, report)
            html = output.read_text(encoding="utf-8")

        for chart in ("success", "outcome", "round"):
            self.assertIn(
                f'data-chart="{chart}" style="width:0.00%',
                html,
            )
        self.assertIn("&mdash;% of measured tokens", html)

    def test_html_mobile_css_prevents_horizontal_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            self.assertEqual(self._build(out, priced=False), 0)
            html = (out / "token_usage.html").read_text(encoding="utf-8")

        self.assertIn(".wrap{min-width:0}", html)
        self.assertIn(
            ".cards,.success-grid,.waste-grid{grid-template-columns:1fr}", html
        )
        self.assertIn(
            ".hero .v,.scenario-value,.tile .v,.meta,.note,.sub{overflow-wrap:anywhere}",
            html,
        )

    def test_empty_run_dir_exits_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            (out / "run").mkdir()
            code = tu.main(["--run-dir", str(out / "run"), "--out-dir", str(out)])
            self.assertEqual(code, 0)
            self.assertFalse((out / "token_usage.json").exists())

    def test_csv_has_one_row_per_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            self._build(out, priced=True)
            rows = (out / "token_usage.csv").read_text().strip().splitlines()
            self.assertEqual(len(rows), 3)  # header + 2 attempts
            self.assertTrue(rows[0].startswith("task,attempt_index,trial"))
            self.assertIn("usage_source", rows[0])
            self.assertIn("measurement_quality", rows[0])


class SessionUsageTest(unittest.TestCase):
    """The xyne-cli adapter's transcript parser."""

    def _write_session(self, root: pathlib.Path, name: str, lines: list) -> None:
        d = root / "encoded-cwd"
        d.mkdir(parents=True, exist_ok=True)
        rendered = [
            entry if isinstance(entry, str) else json.dumps(entry)
            for entry in lines
        ]
        (d / name).write_text("\n".join(rendered) + "\n")

    def test_sums_assistant_usage_and_skips_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = pathlib.Path(tmp) / "sessions"
            self._write_session(sessions, "a.jsonl", [
                {"sessionId": "abc", "cwd": "/app"},          # SessionHeader
                {"role": "user", "content": "do the thing"},
                {"role": "assistant", "usage": {
                    "input": 100, "output": 10,
                    "cacheRead": 500, "cacheWrite": 50,
                    "cost": {"total": 0.25}}},
                {"role": "assistant", "usage": {
                    "input": 200, "output": 20,
                    "cacheRead": 0, "cacheWrite": 0,
                    "cost": {"total": 0.25}}},
            ])
            totals = sum_session_usage(sessions)
            self.assertIsNotNone(totals)
            self.assertEqual(totals["messages"], 2)
            self.assertEqual(totals["files"], 1)
            self.assertEqual(totals["input"], 300)
            self.assertEqual(totals["output"], 30)
            self.assertEqual(totals["cacheRead"], 500)
            self.assertEqual(totals["cacheWrite"], 50)
            self.assertAlmostEqual(totals["cost"], 0.5)

    def test_sums_xyne_0_3_4_nested_message_usage(self) -> None:
        """Current xyne JSONL wraps role and usage under `message`."""
        with tempfile.TemporaryDirectory() as tmp:
            sessions = pathlib.Path(tmp) / "sessions"
            self._write_session(sessions, "a.jsonl", [
                {"type": "session", "id": "abc", "cwd": "/app"},
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "usage": {
                            "input": 120,
                            "output": 30,
                            "cacheRead": 400,
                            "cacheWrite": 50,
                            "cost": {"total": 0},
                        },
                    },
                },
            ])

            totals = sum_session_usage(sessions)

            self.assertIsNotNone(totals)
            self.assertEqual(totals["messages"], 1)
            self.assertEqual(totals["input"], 120)
            self.assertEqual(totals["output"], 30)
            self.assertEqual(totals["cacheRead"], 400)
            self.assertEqual(totals["cacheWrite"], 50)

    def test_harbor_mapping_folds_cache_into_input(self) -> None:
        """harbor's n_input_tokens is input INCLUDING cache."""
        totals = {"input": 300, "output": 30, "cacheRead": 500,
                  "cacheWrite": 50, "cost": 0.5, "files": 1, "messages": 2}
        fields = to_harbor_fields(totals)
        self.assertEqual(fields["n_input_tokens"], 850)   # 300 + 500 + 50
        self.assertEqual(fields["n_cache_tokens"], 500)   # cache reads only
        self.assertEqual(fields["n_output_tokens"], 30)
        self.assertAlmostEqual(fields["cost_usd"], 0.5)

    def test_zero_cost_becomes_none(self) -> None:
        """grid.ai self-hosted models price at 0 — that is 'unpriced', not free."""
        totals = {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0,
                  "cost": 0.0, "files": 1, "messages": 1}
        self.assertIsNone(to_harbor_fields(totals)["cost_usd"])

    def test_truncated_final_line_is_tolerated(self) -> None:
        """A trial killed on timeout leaves a half-written last line."""
        with tempfile.TemporaryDirectory() as tmp:
            sessions = pathlib.Path(tmp) / "sessions"
            self._write_session(sessions, "a.jsonl", [
                {"role": "assistant", "usage": {"input": 10, "output": 1,
                                                "cacheRead": 0, "cacheWrite": 0}},
                '{"role": "assistant", "usage": {"input": 99',   # truncated
            ])
            totals = sum_session_usage(sessions)
            self.assertEqual(totals["input"], 10)
            self.assertEqual(totals["messages"], 1)

    def test_multiple_session_files_are_summed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = pathlib.Path(tmp) / "sessions"
            for name in ("a.jsonl", "b.jsonl"):
                self._write_session(sessions, name, [
                    {"role": "assistant", "usage": {"input": 10, "output": 1,
                                                    "cacheRead": 0,
                                                    "cacheWrite": 0}},
                ])
            totals = sum_session_usage(sessions)
            self.assertEqual(totals["files"], 2)
            self.assertEqual(totals["input"], 20)

    def test_missing_dir_and_no_usage_return_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.assertIsNone(sum_session_usage(root / "sessions"))
            sessions = root / "sessions"
            self._write_session(sessions, "a.jsonl", [
                {"sessionId": "abc"},
                {"role": "user", "content": "hi"},
            ])
            # Files present but no assistant usage: unmeasured, not zero.
            self.assertIsNone(sum_session_usage(sessions))

    def test_bool_is_not_counted_as_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = pathlib.Path(tmp) / "sessions"
            self._write_session(sessions, "a.jsonl", [
                {"role": "assistant", "usage": {"input": True, "output": 5,
                                                "cacheRead": 0, "cacheWrite": 0}},
            ])
            self.assertEqual(sum_session_usage(sessions)["input"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
