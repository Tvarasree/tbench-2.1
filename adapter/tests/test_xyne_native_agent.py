#!/usr/bin/env python3
"""Contract for the Terminal-Bench xyne-cli NATIVE-harness adapter.

The value of this agent is entirely that it runs a DIFFERENT engine from
`xyne-cli`. Every test here defends one of the three things that make that
claim checkable rather than assumed:

  * the run command actually carries XYNE_NATIVE_HARNESS=1 and logs it,
  * install() refuses a binary that ignores the flag,
  * the session reader distinguishes the two engines and reads the native
    token ledger (the embedded reader would report zero).
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock


ADAPTER_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADAPTER_ROOT))

from xyne_native_harbor_agent.session_usage import (  # noqa: E402
    ENGINE_EMBEDDED,
    ENGINE_NATIVE,
    classify_session_file,
    scan_engines,
    sum_session_usage,
    to_harbor_fields,
)


NATIVE_HEADER = {
    "type": "xyne-native-session",
    "version": 1,
    "id": "s1",
    "timestamp": "2026-09-09T00:00:00.000Z",
    "cwd": "/app",
}
EMBEDDED_HEADER = {"type": "session", "id": "p1", "cwd": "/app"}


def _module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def _install_fake_harbor() -> None:
    _module("harbor")
    _module("harbor.agents")
    _module("harbor.agents.installed")
    installed_base = _module("harbor.agents.installed.base")
    _module("harbor.environments")
    environments_base = _module("harbor.environments.base")
    _module("harbor.models")
    _module("harbor.models.agent")
    agent_context = _module("harbor.models.agent.context")

    class BaseInstalledAgent:
        _parsed_model_name = None
        logger = logging.getLogger("test_xyne_native_agent")

        def _get_env(self, name: str) -> str | None:
            # Harbor 0.13.1 returns None for an absent variable. Returning ""
            # here would hide exactly the None-vs-empty bug that broke the
            # real eval run.
            return os.environ.get(name)

    installed_base.BaseInstalledAgent = BaseInstalledAgent
    installed_base.with_prompt_template = lambda function: function
    environments_base.BaseEnvironment = object
    agent_context.AgentContext = object


def _write_jsonl(path: pathlib.Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )


def _ledger(inp: int, out: int, cache_read: int = 0, cache_write: int = 0) -> dict:
    return {
        "type": "xyne-native-session-entry",
        "kind": "llm_usage",
        "sessionId": "s1",
        "at": "2026-09-09T00:00:01.000Z",
        "data": {
            "usage": {
                "inputTokens": inp,
                "outputTokens": out,
                "cacheReadTokens": cache_read,
                "cacheWriteTokens": cache_write,
            },
            "finishReason": "stop",
        },
    }


class NativeAgentCommandTest(unittest.TestCase):
    def tearDown(self) -> None:
        for name in tuple(sys.modules):
            if name == "harbor" or name.startswith("harbor."):
                sys.modules.pop(name, None)
        sys.modules.pop("xyne_native_harbor_agent.agent", None)

    def _agent(self):
        _install_fake_harbor()
        module = importlib.import_module("xyne_native_harbor_agent.agent")
        return module, module.XyneNativeCliAgent()

    def test_run_sets_and_logs_the_native_harness_flag(self) -> None:
        """The whole point of this agent: the flag must reach the container.

        Catches a silent regression to the embedded engine — the failure mode
        with no symptom, where every task still runs and every number is about
        the wrong runtime.
        """
        module, agent = self._agent()
        commands: list[str] = []

        async def exec_as_root(environment: object, command: str) -> None:
            commands.append(command)

        agent.exec_as_root = exec_as_root
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JUSPAY_API_KEY", None)
            os.environ.pop("XYNE_API_KEY", None)
            os.environ.pop("SWE_TRACE", None)
            asyncio.run(agent.run("repair /app", object(), object()))

        self.assertEqual(len(commands), 1)
        command = commands[0]
        # The flag must prefix the actual invocation, not merely appear
        # somewhere in the line (the echo alone would satisfy a loose check).
        self.assertIn("XYNE_NATIVE_HARNESS=1 xyne prompt", command)
        self.assertNotIn("XYNE_NATIVE_PROFILE", command)
        # --yolo is mandatory: without it every mutating tool call stalls at
        # the permission gate and no task can be solved.
        self.assertIn("--yolo", command)
        # No --tools allow-list: absence means "activate everything registered".
        self.assertNotIn("--tools", command)
        # The flag must be greppable in the per-trial log, not just in run.sh.
        self.assertIn("[tb-native]", command)
        self.assertIn("tee /logs/agent/xyne.log", command)

    def test_run_leaves_jev_off_without_a_key(self) -> None:
        """No JUSPAY_API_KEY -> the command must stay byte-identical to the
        pre-Jev shape: no JEV_XOR_DECIDER flag, no jev.env sourcing."""
        module, agent = self._agent()
        commands: list[str] = []

        async def exec_as_root(environment: object, command: str) -> None:
            commands.append(command)

        agent.exec_as_root = exec_as_root
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JUSPAY_API_KEY", None)
            os.environ.pop("XYNE_API_KEY", None)
            os.environ.pop("SWE_TRACE", None)
            asyncio.run(agent.run("repair /app", object(), object()))

        command = commands[0]
        self.assertNotIn("JEV_XOR_DECIDER", command)
        self.assertNotIn("jev.env", command)
        self.assertNotIn("SWE_TRACE", command)

    def test_run_arms_jev_with_key_but_never_inlines_it(self) -> None:
        """With a key: JEV_XOR_DECIDER=1 prefixes xyne, the uploaded env
        file is sourced, and the key VALUE never appears in the command."""
        module, agent = self._agent()
        commands: list[str] = []

        async def exec_as_root(environment: object, command: str) -> None:
            commands.append(command)

        agent.exec_as_root = exec_as_root
        with mock.patch.dict(
            os.environ, {"JUSPAY_API_KEY": "tb-jev-secret"}
        ):
            os.environ.pop("SWE_TRACE", None)
            asyncio.run(agent.run("repair /app", object(), object()))

        command = commands[0]
        self.assertIn("JEV_XOR_DECIDER=1", command)
        self.assertIn("JEV_XOR_EVAL_DIR=/logs/agent/jev-xor", command)
        self.assertIn("xyne prompt", command)
        self.assertNotIn("JEV_READ_SELECTOR", command)
        self.assertIn("set -a; . /root/.xyne/agent/jev.env; set +a", command)
        # The echo must record that Jev (and tracing) were armed, greppable
        # in the log.
        self.assertIn(
            "JEV_XOR_DECIDER=1 JEV_XOR_EVAL_DIR=/logs/agent/jev-xor SWE_TRACE=1",
            command,
        )
        # Tracing follows Jev by default: payloads land in /logs/agent.
        self.assertIn("SWE_TRACE=1", command)
        self.assertIn("SWE_TRACE_DIR=/logs/agent/swe-trace", command)
        # The key value must never leak into the command line (which both
        # harbor's debug logger and the tee'd xyne.log capture).
        self.assertNotIn("tb-jev-secret", command)

    def test_jev_opt_out_overrides_key_presence(self) -> None:
        """JEV_XOR_DECIDER=0 forces the deterministic path for A/B runs."""
        module, agent = self._agent()
        commands: list[str] = []

        async def exec_as_root(environment: object, command: str) -> None:
            commands.append(command)

        agent.exec_as_root = exec_as_root
        with mock.patch.dict(
            os.environ,
            {"JUSPAY_API_KEY": "tb-jev-secret", "JEV_XOR_DECIDER": "0"},
        ):
            asyncio.run(agent.run("repair /app", object(), object()))

        command = commands[0]
        self.assertNotIn("JEV_XOR_DECIDER=1", command)
        self.assertNotIn("jev.env", command)
        self.assertNotIn("SWE_TRACE=1", command)

    def test_swe_trace_opt_out_keeps_jev_but_drops_traces(self) -> None:
        """SWE_TRACE=0 disables call tracing while Jev stays armed."""
        module, agent = self._agent()
        commands: list[str] = []

        async def exec_as_root(environment: object, command: str) -> None:
            commands.append(command)

        agent.exec_as_root = exec_as_root
        with mock.patch.dict(
            os.environ,
            {"JUSPAY_API_KEY": "tb-jev-secret", "SWE_TRACE": "0"},
        ):
            asyncio.run(agent.run("repair /app", object(), object()))

        command = commands[0]
        self.assertIn(
            "JEV_XOR_DECIDER=1 JEV_XOR_EVAL_DIR=/logs/agent/jev-xor xyne prompt",
            command,
        )
        self.assertNotIn("JEV_READ_SELECTOR", command)
        self.assertNotIn("SWE_TRACE=1", command)

    def test_swe_trace_dir_override(self) -> None:
        """SWE_TRACE_DIR relocates the trace directory."""
        module, agent = self._agent()
        commands: list[str] = []

        async def exec_as_root(environment: object, command: str) -> None:
            commands.append(command)

        agent.exec_as_root = exec_as_root
        with mock.patch.dict(
            os.environ,
            {
                "JUSPAY_API_KEY": "tb-jev-secret",
                "SWE_TRACE_DIR": "/logs/agent/custom-trace",
            },
        ):
            asyncio.run(agent.run("repair /app", object(), object()))

        command = commands[0]
        self.assertIn("SWE_TRACE_DIR=/logs/agent/custom-trace", command)
        self.assertNotIn("SWE_TRACE_DIR=/logs/agent/swe-trace", command)

    def test_swe_trace_forced_on_without_jev(self) -> None:
        """SWE_TRACE=1 collects LLM/tool traces even when Jev is off."""
        module, agent = self._agent()
        commands: list[str] = []

        async def exec_as_root(environment: object, command: str) -> None:
            commands.append(command)

        agent.exec_as_root = exec_as_root
        with mock.patch.dict(os.environ, {"SWE_TRACE": "1"}):
            os.environ.pop("JUSPAY_API_KEY", None)
            os.environ.pop("XYNE_API_KEY", None)
            asyncio.run(agent.run("repair /app", object(), object()))

        command = commands[0]
        self.assertIn("SWE_TRACE=1", command)
        self.assertNotIn("JEV_XOR_DECIDER=1", command)

    def test_run_arms_jev_with_main_model_key_fallback(self) -> None:
        """XYNE_API_KEY alone (the key every eval already passes) arms Jev."""
        module, agent = self._agent()
        commands: list[str] = []

        async def exec_as_root(environment: object, command: str) -> None:
            commands.append(command)

        agent.exec_as_root = exec_as_root
        with mock.patch.dict(os.environ, {"XYNE_API_KEY": "tb-main-key"}):
            os.environ.pop("JUSPAY_API_KEY", None)
            asyncio.run(agent.run("repair /app", object(), object()))

        command = commands[0]
        self.assertIn("JEV_XOR_DECIDER=1", command)
        self.assertIn("xyne prompt", command)
        self.assertIn("set -a; . /root/.xyne/agent/jev.env; set +a", command)
        # The key value still never appears in the command line.
        self.assertNotIn("tb-main-key", command)

    def test_jev_key_prefers_juspay_over_main_model_key(self) -> None:
        """JUSPAY_API_KEY wins when both are set; XYNE_API_KEY is fallback."""
        module, agent = self._agent()
        with mock.patch.dict(
            os.environ,
            {"JUSPAY_API_KEY": "jev-specific", "XYNE_API_KEY": "main-key"},
        ):
            self.assertEqual(agent._jev_api_key(), "jev-specific")
        with mock.patch.dict(os.environ, {"XYNE_API_KEY": "main-key"}):
            os.environ.pop("JUSPAY_API_KEY", None)
            self.assertEqual(agent._jev_api_key(), "main-key")

    def test_install_uploads_jev_env_file_with_key(self) -> None:
        """install() writes the key to /root/.xyne/agent/jev.env (mode 600)
        via a host temp file, keeping it out of every command string."""
        module, agent = self._agent()
        uploaded: list[tuple[str, str]] = []
        execed: list[str] = []

        async def exec_as_root(environment: object, command: str) -> None:
            execed.append(command)

        async def detect_arch(environment: object) -> str:
            return "x64"

        agent.exec_as_root = exec_as_root
        agent._detect_container_arch = detect_arch
        agent._api_key = lambda: "tb-main-key"

        class FakeEnv:
            async def upload_file(self, local: str, remote: str) -> None:
                # Capture contents now; the temp file is deleted on return.
                uploaded.append((pathlib.Path(local).read_text(), remote))

        with tempfile.TemporaryDirectory() as tmpdir:
            (pathlib.Path(tmpdir) / "xyne-linux-x64").write_bytes(b"fake")
            (pathlib.Path(tmpdir) / "package.json").write_text("{}")
            with mock.patch.dict(
                os.environ,
                {
                    "XYNE_NATIVE_BINARY_DIR": tmpdir,
                    "JUSPAY_API_KEY": "tb-jev-secret",
                    "JEV_MODEL": "jev-latest",
                },
            ):
                asyncio.run(agent.install(FakeEnv()))

        remote_paths = [remote for _, remote in uploaded]
        self.assertIn("/root/.xyne/agent/jev.env", remote_paths)
        jev_content = next(
            content
            for content, remote in uploaded
            if remote == "/root/.xyne/agent/jev.env"
        )
        self.assertIn("JUSPAY_API_KEY=", jev_content)
        self.assertIn("tb-jev-secret", jev_content)
        self.assertIn("JEV_MODEL=jev-latest", jev_content)
        # The file is locked down and never referenced with its value inline.
        self.assertIn("chmod 600 /root/.xyne/agent/jev.env", execed)
        for command in execed:
            self.assertNotIn("tb-jev-secret", command)

    def test_agent_name_is_distinct_from_the_embedded_adapter(self) -> None:
        module, agent = self._agent()
        self.assertEqual(module.XyneNativeCliAgent.name(), "xyne-cli-native")
        self.assertTrue(str(agent._binary_dir()).endswith("binaries-native"))


class NativeSessionUsageTest(unittest.TestCase):
    def test_header_line_identifies_the_engine(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            native = root / "a.jsonl"
            embedded = root / "b.jsonl"
            _write_jsonl(native, [NATIVE_HEADER, _ledger(10, 5)])
            _write_jsonl(embedded, [EMBEDDED_HEADER])
            self.assertEqual(classify_session_file(native), ENGINE_NATIVE)
            self.assertEqual(classify_session_file(embedded), ENGINE_EMBEDDED)

            scan = scan_engines(root)
            self.assertEqual(scan["verdict"], "mixed")
            self.assertEqual(scan["native_files"], 1)
            self.assertEqual(scan["embedded_files"], 1)

    def test_verdict_is_embedded_when_the_flag_did_not_take(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            _write_jsonl(root / "b.jsonl", [EMBEDDED_HEADER])
            self.assertEqual(scan_engines(root)["verdict"], "embedded")

    def test_sums_the_llm_usage_ledger_across_nested_dirs(self) -> None:
        """Native sessions live under sessions/<encoded-cwd>/, not the root."""
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            nested = root / "-app"
            nested.mkdir()
            _write_jsonl(
                nested / "a.jsonl",
                [NATIVE_HEADER, _ledger(100, 20, 300, 40), _ledger(5, 1)],
            )
            totals = sum_session_usage(root)
            assert totals is not None
            self.assertEqual(totals["ledger_rows"], 2)
            self.assertEqual(totals["inputTokens"], 105)
            self.assertEqual(totals["outputTokens"], 21)
            self.assertEqual(totals["cacheReadTokens"], 300)
            self.assertEqual(totals["cacheWriteTokens"], 40)

            fields = to_harbor_fields(totals)
            # harbor's n_input_tokens includes cache read + write.
            self.assertEqual(fields["n_input_tokens"], 105 + 300 + 40)
            self.assertEqual(fields["n_cache_tokens"], 300)
            self.assertEqual(fields["n_output_tokens"], 21)
            # Native ledger rows carry no cost; unpriced must stay None so the
            # reporter's --price-* inputs supply the money figures.
            self.assertIsNone(fields["cost_usd"])

    def test_embedded_sessions_never_contribute(self) -> None:
        """A shared directory must not let pi's counters leak into native totals."""
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            _write_jsonl(
                root / "pi.jsonl",
                [
                    EMBEDDED_HEADER,
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "usage": {"input": 999, "output": 999},
                        },
                    },
                ],
            )
            self.assertIsNone(sum_session_usage(root))

    def test_ledger_rows_suppress_the_legacy_fallback(self) -> None:
        """Mirrors sumNativeUsage: never add both, or a mixed log double counts."""
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            _write_jsonl(
                root / "a.jsonl",
                [
                    NATIVE_HEADER,
                    _ledger(100, 20),
                    {
                        "type": "xyne-native-session-entry",
                        "kind": "assistant_message",
                        "data": {"usage": {"inputTokens": 7, "outputTokens": 3}},
                    },
                ],
            )
            totals = sum_session_usage(root)
            assert totals is not None
            self.assertEqual(totals["inputTokens"], 100)
            self.assertEqual(totals["outputTokens"], 20)
            self.assertEqual(totals["fallback_messages"], 0)

    def test_legacy_assistant_usage_used_only_without_a_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            _write_jsonl(
                root / "a.jsonl",
                [
                    NATIVE_HEADER,
                    {
                        "type": "xyne-native-session-entry",
                        "kind": "assistant_message",
                        "data": {"usage": {"inputTokens": 7, "outputTokens": 3}},
                    },
                ],
            )
            totals = sum_session_usage(root)
            assert totals is not None
            self.assertEqual(totals["fallback_messages"], 1)
            self.assertEqual(totals["inputTokens"], 7)

    def test_truncated_final_line_does_not_lose_earlier_rows(self) -> None:
        """A trial killed on timeout leaves a half-written last line."""
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            path = root / "a.jsonl"
            path.write_text(
                json.dumps(NATIVE_HEADER) + "\n"
                + json.dumps(_ledger(50, 10)) + "\n"
                + '{"type":"xyne-native-session-entry","kind":"llm_u',
                encoding="utf-8",
            )
            totals = sum_session_usage(root)
            assert totals is not None
            self.assertEqual(totals["ledger_rows"], 1)
            self.assertEqual(totals["inputTokens"], 50)

    def test_absent_or_unmeasured_dir_reports_nothing_rather_than_zero(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            self.assertIsNone(sum_session_usage(root / "missing"))
            _write_jsonl(root / "a.jsonl", [NATIVE_HEADER])
            self.assertIsNone(sum_session_usage(root))
            self.assertEqual(scan_engines(root / "missing")["verdict"], "none")


if __name__ == "__main__":
    unittest.main()
