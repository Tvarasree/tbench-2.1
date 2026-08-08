#!/usr/bin/env python3
"""Tests for the container-side Pi provider and invocation contract."""
from __future__ import annotations

import pathlib
import sys
import unittest


ADAPTER_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADAPTER_ROOT))

from pi_harbor_agent.runtime import (  # noqa: E402
    PI_PACKAGE,
    build_install_command,
    build_models_config,
    build_run_command,
    build_settings,
)


class PiRuntimeTest(unittest.TestCase):
    def test_grid_provider_is_explicit_openai_completions_config(self) -> None:
        """Catches routing a Grid key through Pi's built-in OpenAI endpoint."""
        config = build_models_config(
            api_key="grid-secret",
            base_url="https://grid.ai.juspay.net/v1/",
            model_id="kimi-k3",
        )

        self.assertEqual(config, {
            "providers": {
                "juspay": {
                    "baseUrl": "https://grid.ai.juspay.net/v1",
                    "api": "openai-completions",
                    "apiKey": "grid-secret",
                    "models": [{"id": "kimi-k3", "name": "kimi-k3"}],
                }
            }
        })
        self.assertEqual(
            build_settings("kimi-k3"),
            {"defaultProvider": "juspay", "defaultModel": "kimi-k3"},
        )

    def test_prompt_is_read_from_stdin_and_never_placed_in_argv(self) -> None:
        """Catches prompts beginning with '-' being interpreted as Pi options."""
        command = build_run_command(
            model_id="kimi-k3",
            prompt_path="/tmp/pi prompt.txt",
            output_path="/logs/agent/pi.txt",
        )

        self.assertIn("--provider juspay --model kimi-k3", command)
        self.assertIn("< '/tmp/pi prompt.txt'", command)
        self.assertNotIn("- You are given", command)
        self.assertIn("set -o pipefail", command)
        self.assertIn("tee /logs/agent/pi.txt", command)

    def test_package_tracks_latest_current_namespace_release(self) -> None:
        """Catches pinning Pi or falling back to the deprecated package scope."""
        self.assertEqual(
            PI_PACKAGE,
            "@earendil-works/pi-coding-agent@latest",
        )

    def test_install_command_bootstraps_node_without_harbor_helpers(self) -> None:
        """Catches relying on Node helpers unavailable in Harbor 0.13.1."""
        command = build_install_command()

        self.assertIn("NVM_DIR=\"${NVM_DIR:-$HOME/.nvm}\"", command)
        self.assertIn("nvm-sh/nvm/v0.40.2/install.sh", command)
        self.assertIn("nvm install 22", command)
        self.assertIn(
            "npm install -g --ignore-scripts "
            "@earendil-works/pi-coding-agent@latest",
            command,
        )
        self.assertTrue(command.endswith("pi --version"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
