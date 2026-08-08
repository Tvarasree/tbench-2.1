#!/usr/bin/env python3
"""Behavioral tests for Pi JSONL transcript validation and accounting."""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest


ADAPTER_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADAPTER_ROOT))

from pi_harbor_agent.transcript import (  # noqa: E402
    TranscriptError,
    analyze_transcript,
    require_healthy_transcript,
)


class PiTranscriptTest(unittest.TestCase):
    def _write(self, root: str, records: list[dict]) -> pathlib.Path:
        path = pathlib.Path(root) / "pi.txt"
        path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        return path

    def test_rejects_error_only_authentication_transcript(self) -> None:
        """Catches accepting Pi's exit-0 API error as completed agent work."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [{
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "error",
                    "errorMessage": "401 Incorrect API key provided",
                    "usage": {"input": 0, "output": 0},
                },
            }])

            summary = analyze_transcript(path)
            self.assertFalse(summary.active)
            self.assertEqual(summary.final_stop_reason, "error")
            with self.assertRaisesRegex(TranscriptError, "401 Incorrect API key"):
                require_healthy_transcript(path)

    def test_rejects_cli_output_without_assistant_message(self) -> None:
        """Catches treating `Unknown option` output as a model attempt."""
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "pi.txt"
            path.write_text("Error: Unknown option: - task\n", encoding="utf-8")

            with self.assertRaisesRegex(TranscriptError, "no assistant messages"):
                require_healthy_transcript(path)

    def test_accepts_real_zero_reward_model_activity(self) -> None:
        """Catches coupling infrastructure health to verifier reward."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, [{
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "I could not solve it."}],
                    "stopReason": "stop",
                    "usage": {
                        "input": 120,
                        "output": 8,
                        "cacheRead": 30,
                        "cacheWrite": 4,
                        "cost": {"total": 0.02},
                    },
                },
            }])

            summary = require_healthy_transcript(path)
            self.assertTrue(summary.active)
            self.assertEqual(summary.input_tokens, 154)
            self.assertEqual(summary.output_tokens, 8)
            self.assertAlmostEqual(summary.cost_usd, 0.02)


if __name__ == "__main__":
    unittest.main(verbosity=2)
