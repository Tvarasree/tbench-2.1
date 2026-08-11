#!/usr/bin/env python3
"""Command contract for the Terminal-Bench xyne-cli adapter."""
from __future__ import annotations

import asyncio
import importlib
import pathlib
import sys
import types
import unittest


ADAPTER_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADAPTER_ROOT))


def _module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


class XyneAgentCommandTest(unittest.TestCase):
    def tearDown(self) -> None:
        for name in tuple(sys.modules):
            if name == "harbor" or name.startswith("harbor."):
                sys.modules.pop(name, None)
        sys.modules.pop("xyne_harbor_agent.agent", None)

    def test_headless_run_lets_xyne_activate_all_registered_tools(self) -> None:
        """Catches reintroducing a restrictive or stale --tools allow-list."""
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
            pass

        installed_base.BaseInstalledAgent = BaseInstalledAgent
        installed_base.with_prompt_template = lambda function: function
        environments_base.BaseEnvironment = object
        agent_context.AgentContext = object

        module = importlib.import_module("xyne_harbor_agent.agent")
        agent = module.XyneCliAgent()
        commands: list[str] = []

        async def exec_as_root(environment: object, command: str) -> None:
            commands.append(command)

        agent.exec_as_root = exec_as_root

        asyncio.run(agent.run("repair /app", object(), object()))

        self.assertEqual(
            commands,
            [
                "xyne prompt 'repair /app' --yolo "
                "2>&1 | tee /logs/agent/xyne.log"
            ],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
