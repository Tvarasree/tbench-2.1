"""Harbor adapter for xyne-cli running against grid.ai (juspay) models.

Uploads a pre-built linux-arm64 xyne binary into the task container and runs
`xyne prompt` on each task. Set XYNE_BINARY_DIR (host path) to override the
default binary location; set XYNE_API_KEY in the host shell or via
--agent-env XYNE_API_KEY=... on the CLI.
"""

import json
import os
import shlex
import tempfile
from pathlib import Path

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from xyne_harbor_agent.session_usage import sum_session_usage, to_harbor_fields


DEFAULT_BINARY_DIR = (
    Path(__file__).resolve().parents[2] / "binaries"
)
DEFAULT_BASE_URL = "https://grid.ai.juspay.net/v1"
DEFAULT_PROVIDER = "juspay"
DEFAULT_MODEL = "private-large"


class XyneCliAgent(BaseInstalledAgent):
    """Run xyne-cli against grid.ai self-hosted models inside a harbor sandbox."""

    @staticmethod
    def name() -> str:
        return "xyne-cli"

    def _binary_dir(self) -> Path:
        return Path(os.environ.get("XYNE_BINARY_DIR", str(DEFAULT_BINARY_DIR)))

    def _model_id(self) -> str:
        # Harbor splits --model "juspay/private-large" into provider+name.
        # Use the parsed model name and fall back to default if missing.
        return self._parsed_model_name or DEFAULT_MODEL

    def _api_key(self) -> str:
        key = self._get_env("XYNE_API_KEY")
        if not key:
            raise RuntimeError(
                "XYNE_API_KEY is required. Pass it via --agent-env "
                "XYNE_API_KEY=... or export it in your shell."
            )
        return key

    def _base_url(self) -> str:
        return self._get_env("XYNE_BASE_URL") or DEFAULT_BASE_URL

    def get_version_command(self) -> str | None:
        return "xyne --version"

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Fill harbor's token/cost fields from xyne's session transcript.

        `install()` symlinks the container's /root/.xyne/agent/sessions into
        /logs/agent/sessions, and harbor downloads the agent dir *before*
        calling this hook (`_download_agent_logs()` then
        `_populate_agent_context()` in harbor/trial/trial.py), so the
        transcripts are already on the host here under both the mounted and the
        DooD-unmounted transfer modes.

        xyne writes no terminal summary record: its own totals are the sum of
        the per-assistant-message `usage` blocks (see `buildStateSnapshot` in
        xyne-cli src/agent/serve.ts), so summing the same way agrees with what
        xyne itself reports. Caveat: subagent turns are only counted if pi
        persisted them into this session file — reconcile against a finished
        run before trusting the absolute numbers.

        Never raises: a token-accounting problem must not fail a graded trial.
        """
        try:
            totals = sum_session_usage(self.logs_dir / "sessions")
        except Exception:  # noqa: BLE001 — accounting must never fail a trial
            self.logger.exception("Failed to sum xyne session usage")
            return

        if totals is None:
            self.logger.debug("No xyne session usage found under %s", self.logs_dir)
            return

        for field, value in to_harbor_fields(totals).items():
            setattr(context, field, value)
        context.metadata = {
            "usage_source": "xyne-session-jsonl",
            "session_files": totals["files"],
            "assistant_messages": totals["messages"],
        }

    async def _detect_container_arch(self, environment: BaseEnvironment) -> str:
        result = await environment.exec(command="uname -m", user="root")
        machine = (result.stdout or "").strip()
        if machine in {"x86_64", "amd64"}:
            return "x64"
        if machine in {"aarch64", "arm64"}:
            return "arm64"
        raise RuntimeError(f"Unsupported container arch: {machine!r}")

    async def install(self, environment: BaseEnvironment) -> None:
        binary_dir = self._binary_dir()
        package_json = binary_dir / "package.json"

        arch = await self._detect_container_arch(environment)
        binary = binary_dir / f"xyne-linux-{arch}"

        if not binary.exists():
            raise RuntimeError(
                f"xyne linux binary not found at {binary}. Build it with: "
                f"cd ~/Paul\\ Tests/xyne-cli && bun run build:binary:linux-"
                f"{'arm' if arch == 'arm64' else 'x64'} "
                f"(and ensure package.json sits next to the binary)."
            )
        if not package_json.exists():
            raise RuntimeError(
                f"package.json missing next to binary at {package_json}. "
                f"Run: cp ~/Paul\\ Tests/xyne-cli/package.json {package_json}"
            )

        # Redirect xyne's session transcripts (getAgentDir()/sessions, i.e.
        # /root/.xyne/agent/sessions/*.jsonl) into the bind-mounted /logs/agent
        # so harbor captures them even when the run is cancelled on timeout.
        # Only `sessions` is symlinked — models.json (API key) stays out of /logs.
        await self.exec_as_root(
            environment,
            "mkdir -p /opt/xyne /root/.xyne/agent /logs/agent/sessions "
            "&& ln -sfn /logs/agent/sessions /root/.xyne/agent/sessions",
        )
        await environment.upload_file(str(binary), "/opt/xyne/xyne")
        await environment.upload_file(str(package_json), "/opt/xyne/package.json")
        await self.exec_as_root(
            environment,
            "chmod +x /opt/xyne/xyne && ln -sf /opt/xyne/xyne /usr/local/bin/xyne",
        )

        model_id = self._model_id()
        models = {
            "providers": {
                DEFAULT_PROVIDER: {
                    "baseUrl": self._base_url(),
                    "api": "openai-completions",
                    "apiKey": self._api_key(),
                    "models": [{"id": model_id, "name": model_id}],
                }
            }
        }
        settings = {
            "defaultProvider": DEFAULT_PROVIDER,
            "defaultModel": model_id,
        }

        # Write each file to a host temp, upload, and delete the temp. This
        # keeps the API key out of command-line strings and env vars that
        # harbor's debug logger would otherwise capture.
        for filename, payload, mode in (
            ("models.json", models, "600"),
            ("settings.json", settings, "644"),
        ):
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            ) as tmp:
                json.dump(payload, tmp)
                tmp_path = tmp.name
            try:
                await environment.upload_file(
                    tmp_path, f"/root/.xyne/agent/{filename}"
                )
            finally:
                os.unlink(tmp_path)
            await self.exec_as_root(
                environment, f"chmod {mode} /root/.xyne/agent/{filename}"
            )

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        escaped = shlex.quote(instruction)
        await self.exec_as_root(
            environment,
            # --yolo is required, not a convenience. Headless `xyne prompt` has
            # no interactive approver, so without it every mutating tool call
            # (write/edit/bash) stalls at the permission gate and xyne exits 1
            # with "a tool call was not executed" — i.e. no terminal-bench task
            # can ever be solved. See handlePromptCommand in xyne-cli
            # src/core/services/cli-parser.ts. Position is free: the flag is
            # filtered out of the prompt text by exact match, and `escaped` is
            # a single shell-quoted argv element. Do not pass --tools: when the
            # option is absent, xyne activates every tool registered in the
            # headless session; supplying it creates a strict allow-list that
            # can become stale as xyne's tool registry changes.
            command=(
                f"xyne prompt {escaped} --yolo "
                f"2>&1 | tee /logs/agent/xyne.log"
            ),
        )
