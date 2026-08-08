#!/usr/bin/env python3
"""Parse and validate Pi's JSONL transcript without depending on Harbor."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path


FAILED_STOP_REASONS = {"error", "aborted"}


class TranscriptError(RuntimeError):
    """Raised when Pi produced no usable model activity."""


@dataclass(frozen=True)
class TranscriptSummary:
    assistant_messages: int
    active_messages: int
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    cost_usd: float
    final_stop_reason: str
    final_error_message: str

    @property
    def active(self) -> bool:
        return (
            self.assistant_messages > 0
            and self.final_stop_reason not in FAILED_STOP_REASONS
            and self.active_messages > 0
        )


def _number(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(int(value), 0)


def _positive_money(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(float(value), 0.0)


def _has_content(content: object) -> bool:
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"toolCall", "tool_call", "toolUse", "tool_use"}:
            return True
        for key in ("text", "thinking"):
            value = block.get(key)
            if isinstance(value, str) and value.strip():
                return True
    return False


def analyze_transcript(path: Path) -> TranscriptSummary:
    assistant_messages = active_messages = 0
    input_tokens = cache_read = cache_write = output_tokens = 0
    cost_usd = 0.0
    final_stop_reason = final_error_message = ""

    if path.is_file():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    else:
        lines = []

    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("type") != "message_end":
            continue
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue

        assistant_messages += 1
        final_stop_reason = str(message.get("stopReason") or "")
        final_error_message = str(message.get("errorMessage") or "")
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
        current_input = _number(usage.get("input"))
        current_read = _number(usage.get("cacheRead"))
        current_write = _number(usage.get("cacheWrite"))
        current_output = _number(usage.get("output"))
        input_tokens += current_input + current_read + current_write
        cache_read += current_read
        cache_write += current_write
        output_tokens += current_output
        cost = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
        cost_usd += _positive_money(cost.get("total"))

        current_tokens = current_input + current_read + current_write + current_output
        if final_stop_reason not in FAILED_STOP_REASONS and (
            current_tokens > 0 or _has_content(message.get("content"))
        ):
            active_messages += 1

    return TranscriptSummary(
        assistant_messages=assistant_messages,
        active_messages=active_messages,
        input_tokens=input_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        final_stop_reason=final_stop_reason,
        final_error_message=final_error_message,
    )


def require_healthy_transcript(path: Path) -> TranscriptSummary:
    summary = analyze_transcript(path)
    if summary.assistant_messages == 0:
        raise TranscriptError("Pi produced no assistant messages")
    if summary.final_stop_reason in FAILED_STOP_REASONS:
        detail = summary.final_error_message or summary.final_stop_reason
        raise TranscriptError(f"Pi final model request failed: {detail}")
    if not summary.active:
        raise TranscriptError("Pi produced no usable model activity")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript", type=Path)
    args = parser.parse_args()
    try:
        summary = require_healthy_transcript(args.transcript)
    except TranscriptError as exc:
        print(f"[pi-health] invalid transcript: {exc}")
        return 2
    print(json.dumps(asdict(summary), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

