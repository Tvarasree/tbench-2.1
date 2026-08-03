#!/usr/bin/env python3
"""Agent-specific raw token source tests."""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from analysis.token_sources import parse_agent_usage


class TokenSourcesTest(unittest.TestCase):
    def _trial(self, tmp: str) -> pathlib.Path:
        trial = pathlib.Path(tmp) / "trial"
        (trial / "agent").mkdir(parents=True)
        return trial

    def _jsonl(self, path: pathlib.Path, records: list[dict]) -> None:
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n")

    def test_opencode_sums_step_finish_cache_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trial = self._trial(tmp)
            self._jsonl(trial / "agent" / "opencode.txt", [
                {"type": "step_finish", "part": {
                    "tokens": {"input": 100, "output": 20,
                               "cache": {"read": 300, "write": 40}},
                    "cost": 0.1,
                }},
                {"type": "step_finish", "part": {
                    "tokens": {"input": 50, "output": 10,
                               "cache": {"read": 0, "write": 5}},
                    "cost": 0.2,
                }},
            ])
            usage = parse_agent_usage(trial, "opencode")
            self.assertEqual(usage.n_input_tokens, 495)
            self.assertEqual(usage.n_cache_read_tokens, 300)
            self.assertEqual(usage.n_cache_write_tokens, 45)
            self.assertEqual(usage.n_output_tokens, 30)
            self.assertAlmostEqual(usage.cost_usd, 0.3)

    def test_pi_sums_message_end_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trial = self._trial(tmp)
            self._jsonl(trial / "agent" / "pi.txt", [{
                "type": "message_end",
                "message": {"role": "assistant", "usage": {
                    "input": 100, "output": 20,
                    "cacheRead": 300, "cacheWrite": 40,
                    "cost": {"total": 0.25},
                }},
            }])
            usage = parse_agent_usage(trial, "pi")
            self.assertEqual(usage.n_input_tokens, 440)
            self.assertEqual(usage.n_cache_read_tokens, 300)
            self.assertEqual(usage.n_cache_write_tokens, 40)
            self.assertEqual(usage.n_output_tokens, 20)

    def test_aider_uses_last_cumulative_token_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trial = self._trial(tmp)
            (trial / "agent" / "aider.txt").write_text(
                "Tokens: 1.0k sent, 200 cache write, 3.0k cache hit, 50 received.\n"
                "Tokens: 2.0k sent, 400 cache write, 5.0k cache hit, 100 received.\n"
                "Cost: $0.20 message, $0.75 session.\n"
            )
            usage = parse_agent_usage(trial, "aider")
            self.assertEqual(usage.n_input_tokens, 7000)
            self.assertEqual(usage.n_cache_read_tokens, 5000)
            self.assertEqual(usage.n_cache_write_tokens, 400)
            self.assertEqual(usage.n_output_tokens, 100)
            self.assertAlmostEqual(usage.cost_usd, 0.75)

    def test_goose_uses_final_complete_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trial = self._trial(tmp)
            self._jsonl(trial / "agent" / "goose.txt", [{
                "type": "complete", "total_tokens": 1120,
                "input_tokens": 1000, "output_tokens": 120,
                "cache_read_input_tokens": 600,
                "cache_write_input_tokens": 100,
                "cost_usd": 0.8,
            }])
            usage = parse_agent_usage(trial, "goose")
            self.assertEqual(usage.n_input_tokens, 1000)
            self.assertEqual(usage.n_cache_read_tokens, 600)
            self.assertEqual(usage.n_cache_write_tokens, 100)
            self.assertEqual(usage.n_output_tokens, 120)

    def test_claude_code_trajectory_retains_cache_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trial = self._trial(tmp)
            (trial / "agent" / "trajectory.json").write_text(json.dumps({
                "final_metrics": {
                    "total_prompt_tokens": 7000,
                    "total_completion_tokens": 100,
                    "total_cached_tokens": 5000,
                    "total_cost_usd": 0.7,
                    "extra": {
                        "total_cache_read_input_tokens": 5000,
                        "total_cache_creation_input_tokens": 400,
                    },
                },
            }))
            usage = parse_agent_usage(trial, "claude-code")
            self.assertEqual(usage.n_input_tokens, 7000)
            self.assertEqual(usage.n_cache_read_tokens, 5000)
            self.assertEqual(usage.n_cache_write_tokens, 400)

    def test_codex_trajectory_uses_prompt_completion_and_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trial = self._trial(tmp)
            (trial / "agent" / "trajectory.json").write_text(json.dumps({
                "final_metrics": {
                    "total_prompt_tokens": 900,
                    "total_completion_tokens": 100,
                    "total_cached_tokens": 600,
                    "total_cost_usd": 0.4,
                },
            }))
            usage = parse_agent_usage(trial, "codex")
            self.assertEqual(usage.n_input_tokens, 900)
            self.assertEqual(usage.n_cache_read_tokens, 600)
            self.assertEqual(usage.n_cache_write_tokens, 0)
            self.assertEqual(usage.n_output_tokens, 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
