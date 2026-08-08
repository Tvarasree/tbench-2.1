"""Harbor adapter that runs Pi against Grid through an explicit provider."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from pi_harbor_agent.runtime import (
    build_install_command,
    build_models_config,
    build_run_command,
    build_settings,
)
from pi_harbor_agent.transcript import analyze_transcript


DEFAULT_BASE_URL = "https://grid.ai.juspay.net/v1"
DEFAULT_MODEL = "private-large"
PROMPT_PATH = "/tmp/pi-grid-instruction.txt"
OUTPUT_PATH = "/logs/agent/pi.txt"
VALIDATOR_PATH = "/opt/pi-grid/transcript.py"


class PiGridAgent(BaseInstalledAgent):
    """Run Pi with Grid configuration and fail closed on model/API errors."""

    @staticmethod
    def name() -> str:
        return "pi"

    def get_version_command(self) -> str | None:
        return ". ~/.nvm/nvm.sh; pi --version"

    def _model_id(self) -> str:
        return self._parsed_model_name or DEFAULT_MODEL

    def _api_key(self) -> str:
        key = self._get_env("PI_GRID_API_KEY")
        if not key:
            raise RuntimeError(
                "PI_GRID_API_KEY is required; pass it through Harbor --agent-env"
            )
        return key

    def _base_url(self) -> str:
        return self._get_env("PI_GRID_BASE_URL") or DEFAULT_BASE_URL

    async def _upload_json(
        self,
        environment: BaseEnvironment,
        *,
        destination: str,
        payload: dict[str, object],
        mode: str,
    ) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            json.dump(payload, tmp)
            local_path = tmp.name
        try:
            await environment.upload_file(local_path, destination)
        finally:
            os.unlink(local_path)
        await self.exec_as_root(environment, f"chmod {mode} {destination}")

    async def install(self, environment: BaseEnvironment) -> None:
        await self.exec_as_root(
            environment,
            command="apt-get update && apt-get install -y curl git",
            env={"DEBIAN_FRONTEND": "noninteractive"},
        )
        await self.exec_as_agent(
            environment,
            command=build_install_command(),
        )
        await self.exec_as_root(
            environment,
            "mkdir -p /root/.pi/agent /opt/pi-grid /logs/agent",
        )

        model_id = self._model_id()
        await self._upload_json(
            environment,
            destination="/root/.pi/agent/models.json",
            payload=build_models_config(
                api_key=self._api_key(),
                base_url=self._base_url(),
                model_id=model_id,
            ),
            mode="600",
        )
        await self._upload_json(
            environment,
            destination="/root/.pi/agent/settings.json",
            payload=build_settings(model_id),
            mode="644",
        )
        await environment.upload_file(
            str(Path(__file__).with_name("transcript.py")),
            VALIDATOR_PATH,
        )
        await self.exec_as_root(environment, f"chmod 755 {VALIDATOR_PATH}")

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
            tmp.write(instruction)
            local_prompt = tmp.name
        try:
            await environment.upload_file(local_prompt, PROMPT_PATH)
        finally:
            os.unlink(local_prompt)
        await self.exec_as_root(environment, f"chmod 600 {PROMPT_PATH}")

        await self.exec_as_agent(
            environment,
            command=build_run_command(
                model_id=self._model_id(),
                prompt_path=PROMPT_PATH,
                output_path=OUTPUT_PATH,
            ),
        )
        await self.exec_as_agent(
            environment,
            command=f"python3 {VALIDATOR_PATH} {OUTPUT_PATH}",
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        summary = analyze_transcript(self.logs_dir / "pi.txt")
        if summary.assistant_messages == 0:
            return
        context.n_input_tokens = summary.input_tokens
        context.n_cache_tokens = (
            summary.cache_read_tokens + summary.cache_write_tokens
        )
        context.n_output_tokens = summary.output_tokens
        context.cost_usd = summary.cost_usd if summary.cost_usd > 0 else None
        context.metadata = {
            "usage_source": "pi-message-end-jsonl",
            "assistant_messages": summary.assistant_messages,
            "active_messages": summary.active_messages,
            "final_stop_reason": summary.final_stop_reason,
        }
