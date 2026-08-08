#!/usr/bin/env python3
"""Compatibility contract for importing the Pi adapter on Harbor 0.13.1."""
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


class PiAgentImportTest(unittest.TestCase):
    def tearDown(self) -> None:
        for name in tuple(sys.modules):
            if name == "harbor" or name.startswith("harbor."):
                sys.modules.pop(name, None)
        sys.modules.pop("pi_harbor_agent.agent", None)

    def test_imports_with_harbor_0131_public_modules(self) -> None:
        """Catches dependencies on Harbor modules absent from pinned 0.13.1."""
        _module("harbor")
        _module("harbor.agents")
        _module("harbor.agents.installed")
        installed_base = _module("harbor.agents.installed.base")
        environments_base = _module("harbor.environments.base")
        _module("harbor.environments")
        _module("harbor.models")
        _module("harbor.models.agent")
        agent_context = _module("harbor.models.agent.context")

        class BaseInstalledAgent:
            pass

        class BaseEnvironment:
            pass

        class AgentContext:
            pass

        installed_base.BaseInstalledAgent = BaseInstalledAgent
        installed_base.with_prompt_template = lambda function: function
        environments_base.BaseEnvironment = BaseEnvironment
        agent_context.AgentContext = AgentContext

        module = importlib.import_module("pi_harbor_agent.agent")

        self.assertEqual(module.PiGridAgent.name(), "pi")

    def test_install_declares_every_nvm_system_dependency(self) -> None:
        """Catches assuming task images already contain git for NVM."""
        _module("harbor")
        _module("harbor.agents")
        _module("harbor.agents.installed")
        installed_base = _module("harbor.agents.installed.base")
        environments_base = _module("harbor.environments.base")
        _module("harbor.environments")
        _module("harbor.models")
        _module("harbor.models.agent")
        agent_context = _module("harbor.models.agent.context")

        class BaseInstalledAgent:
            pass

        installed_base.BaseInstalledAgent = BaseInstalledAgent
        installed_base.with_prompt_template = lambda function: function
        environments_base.BaseEnvironment = object
        agent_context.AgentContext = object

        module = importlib.import_module("pi_harbor_agent.agent")
        agent = module.PiGridAgent()
        root_commands: list[tuple[str, dict[str, str] | None]] = []
        agent_commands: list[str] = []

        async def exec_as_root(
            environment: object,
            command: str,
            env: dict[str, str] | None = None,
        ) -> None:
            root_commands.append((command, env))

        async def exec_as_agent(environment: object, command: str) -> None:
            agent_commands.append(command)

        async def upload_json(*args: object, **kwargs: object) -> None:
            return None

        class Environment:
            async def upload_file(self, source: str, destination: str) -> None:
                return None

        agent.exec_as_root = exec_as_root
        agent.exec_as_agent = exec_as_agent
        agent._upload_json = upload_json
        agent._model_id = lambda: "kimi-k3"
        agent._api_key = lambda: "grid-secret"
        agent._base_url = lambda: "https://grid.ai.juspay.net/v1"

        asyncio.run(agent.install(Environment()))

        self.assertEqual(
            root_commands[0],
            (
                "apt-get update && apt-get install -y curl git",
                {"DEBIAN_FRONTEND": "noninteractive"},
            ),
        )
        self.assertIn("pi-coding-agent@latest", agent_commands[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
