"""Normalize authoritative raw token artifacts emitted by supported agents."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class Usage:
    n_input_tokens: int
    n_cache_read_tokens: int
    n_cache_write_tokens: int
    n_output_tokens: int
    cost_usd: float | None
    source: str
    quality: str = "full"


def _number(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(int(value), 0)


def _money(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _json_lines(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _parse_opencode(agent_dir: Path) -> Usage | None:
    input_tokens = output_tokens = cache_read = cache_write = 0
    cost = 0.0
    measured = False
    for event in _json_lines(agent_dir / "opencode.txt"):
        if event.get("type") != "step_finish":
            continue
        part = event.get("part")
        if not isinstance(part, dict):
            continue
        tokens = part.get("tokens")
        if not isinstance(tokens, dict):
            continue
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        current_input = _number(tokens.get("input"))
        current_output = _number(tokens.get("output"))
        current_read = _number(cache.get("read"))
        current_write = _number(cache.get("write"))
        input_tokens += current_input + current_read + current_write
        output_tokens += current_output
        cache_read += current_read
        cache_write += current_write
        cost += _money(part.get("cost")) or 0.0
        measured = True
    if not measured:
        return None
    return Usage(input_tokens, cache_read, cache_write, output_tokens,
                 cost or None, "opencode-step-finish-jsonl")


def _parse_pi(agent_dir: Path) -> Usage | None:
    input_tokens = output_tokens = cache_read = cache_write = 0
    cost = 0.0
    measured = False
    for event in _json_lines(agent_dir / "pi.txt"):
        message = event.get("message")
        if event.get("type") != "message_end" or not isinstance(message, dict):
            continue
        if message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        current_input = _number(usage.get("input"))
        current_read = _number(usage.get("cacheRead"))
        current_write = _number(usage.get("cacheWrite"))
        input_tokens += current_input + current_read + current_write
        output_tokens += _number(usage.get("output"))
        cache_read += current_read
        cache_write += current_write
        usage_cost = usage.get("cost")
        if isinstance(usage_cost, dict):
            cost += _money(usage_cost.get("total")) or 0.0
        measured = True
    if not measured:
        return None
    return Usage(input_tokens, cache_read, cache_write, output_tokens,
                 cost or None, "pi-message-end-jsonl")


_TOKEN_LINE = re.compile(
    r"Tokens:\s*(?P<sent>[\d,.]+[kKmM]?)\s+sent"
    r"(?:,\s*(?P<write>[\d,.]+[kKmM]?)\s+cache write)?"
    r"(?:,\s*(?P<read>[\d,.]+[kKmM]?)\s+cache hit)?"
    r",\s*(?P<received>[\d,.]+[kKmM]?)\s+received\."
)
_SESSION_COST = re.compile(r"\$[\d.]+\s+message,\s*\$(?P<cost>[\d.]+)\s+session")


def _formatted_tokens(value: str | None) -> int:
    if not value:
        return 0
    normalized = value.replace(",", "")
    multiplier = 1
    if normalized[-1:].lower() == "k":
        normalized, multiplier = normalized[:-1], 1_000
    elif normalized[-1:].lower() == "m":
        normalized, multiplier = normalized[:-1], 1_000_000
    try:
        return max(round(float(normalized) * multiplier), 0)
    except ValueError:
        return 0


def _parse_aider(agent_dir: Path) -> Usage | None:
    path = agent_dir / "aider.txt"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    matches = list(_TOKEN_LINE.finditer(text))
    if not matches:
        return None
    last = matches[-1]
    sent = _formatted_tokens(last.group("sent"))
    cache_read = _formatted_tokens(last.group("read"))
    cache_write = _formatted_tokens(last.group("write"))
    output = _formatted_tokens(last.group("received"))
    costs = list(_SESSION_COST.finditer(text))
    cost = float(costs[-1].group("cost")) if costs else None
    # Aider's cumulative "sent" includes cache writes but excludes cache hits.
    return Usage(sent + cache_read, cache_read, cache_write, output, cost,
                 "aider-cumulative-token-line")


def _parse_goose(agent_dir: Path) -> Usage | None:
    complete = [
        event for event in _json_lines(agent_dir / "goose.txt")
        if event.get("type") == "complete"
    ]
    if not complete:
        return None
    event = complete[-1]
    if not any(key in event for key in ("input_tokens", "output_tokens")):
        return None
    return Usage(
        _number(event.get("input_tokens")),
        _number(event.get("cache_read_input_tokens")),
        _number(event.get("cache_write_input_tokens")),
        _number(event.get("output_tokens")),
        _money(event.get("cost_usd")),
        "goose-complete-jsonl",
    )


def _parse_trajectory(agent_dir: Path, agent: str) -> Usage | None:
    path = agent_dir / "trajectory.json"
    if not path.is_file():
        return None
    try:
        trajectory = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except ValueError:
        return None
    metrics = trajectory.get("final_metrics") if isinstance(trajectory, dict) else None
    if not isinstance(metrics, dict):
        return None
    if not any(
        isinstance(metrics.get(key), (int, float))
        for key in ("total_prompt_tokens", "total_completion_tokens")
    ):
        return None
    extra = metrics.get("extra") if isinstance(metrics.get("extra"), dict) else {}
    cache_read = _number(
        extra.get("total_cache_read_input_tokens", metrics.get("total_cached_tokens"))
    )
    cache_write = _number(extra.get("total_cache_creation_input_tokens"))
    return Usage(
        _number(metrics.get("total_prompt_tokens")),
        cache_read,
        cache_write,
        _number(metrics.get("total_completion_tokens")),
        _money(metrics.get("total_cost_usd")),
        f"{agent}-atif-trajectory",
    )


_PARSERS: dict[str, Callable[[Path], Usage | None]] = {
    "opencode": _parse_opencode,
    "pi": _parse_pi,
    "aider": _parse_aider,
    "goose": _parse_goose,
}


def parse_agent_usage(trial_dir: Path, agent: str) -> Usage | None:
    """Return the best raw measurement for an agent trial, if available."""
    agent_dir = trial_dir / "agent"
    if agent in {"claude-code", "codex"}:
        return _parse_trajectory(agent_dir, agent)
    parser = _PARSERS.get(agent)
    return parser(agent_dir) if parser else None
