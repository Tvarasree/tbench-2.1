"""Harbor adapter for xyne-cli on its NATIVE plugin-kernel engine.

Identical container plumbing to `xyne_harbor_agent`, but the binary comes from
`binaries-native/` (built from the xyne-cli `feat/native-harness` branch by
setup.sh) and every `xyne` invocation carries `XYNE_NATIVE_HARNESS=1`.

Proving the engine actually switched
------------------------------------
`XYNE_NATIVE_HARNESS=1` is a no-op on a binary built before the kernel landed:
the flag is simply unknown, the embedded-Pi engine runs, and NOTHING in the
output says so. A whole sweep could silently measure the wrong engine. Three
independent checks close that hole:

1. `install()` runs a zero-token probe with a deliberately invalid
   `XYNE_NATIVE_PROFILE`. A kernel-capable binary rejects it by name before
   any model call; anything else fails differently. Raises on mismatch, so a
   stale binary cannot reach a graded task.
2. `run()` echoes both env vars into `/logs/agent/xyne.log` ahead of the turn,
   so the dashboard log viewer shows what was set.
3. `populate_context_post_run()` reads the session log's header line — the two
   engines write different, deliberately incompatible headers — and records
   the verdict in `/logs/agent/engine.json` and on harbor's AgentContext.
   This is evidence produced BY the graded run, not an assertion about it.

Set XYNE_NATIVE_BINARY_DIR (host path) to override the binary location; set
XYNE_API_KEY in the host shell or via --agent-env XYNE_API_KEY=... on the CLI.

Jev-guided reads
----------------
Jev is armed by the same grid.ai credential you already pass for the main
model: JUSPAY_API_KEY when set, otherwise XYNE_API_KEY (both endpoints live
on grid.ai.juspay.net and accept the same key). install() uploads the key to
/root/.xyne/agent/jev.env (mode 600) and run() sources that file before
every prompt, so the key never appears in a command string or the tee'd
/logs/agent/xyne.log. Set JEV_READ_SELECTOR=0 in the host environment to
force the deterministic read path even when a key is installed (for A/B
runs). JEV_SYSTEMONE_ENDPOINT and JEV_MODEL pass through to the container
when set in the host environment.

Full call tracing follows Jev by default: SWE_TRACE=1 is set on every prompt
and SWE_TRACE_DIR defaults to /logs/agent/swe-trace, so harbor downloads the
exact Jev payloads/responses/errors, LLM requests/responses, tool outputs,
navigation log, and run summary alongside the session transcripts. Set
SWE_TRACE=0 in the host environment to opt out; set SWE_TRACE_DIR to relocate
the trace directory.
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from pathlib import Path

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from xyne_native_harbor_agent.session_usage import (
    ENGINE_NATIVE,
    scan_engines,
    sum_session_usage,
    to_harbor_fields,
)


DEFAULT_BINARY_DIR = (
    Path(__file__).resolve().parents[2] / "binaries-native"
)
DEFAULT_BASE_URL = "https://grid.ai.juspay.net/v1"
DEFAULT_PROVIDER = "juspay"
DEFAULT_MODEL = "private-large"

# The kernel engine and the profile it boots. `standard` is the selector's own
# default; naming it explicitly keeps the log line self-describing and pins the
# profile even if that default changes upstream.
NATIVE_ENV = "XYNE_NATIVE_HARNESS=1"
NATIVE_PROFILE = "standard"

# Install-time positive control. `resolveNativeProfileName` accepts only
# {standard, minimal} and rejects anything else by name — but ONLY when the
# kernel is present to read the flag at all. Deliberately invalid.
PROBE_PROFILE = "__tb_native_probe__"
PROBE_EXPECTED = "is not a known native profile"

# Jev read-selector wiring. The key is uploaded as a shell-sourceable file
# (never inline in a command) and sourced before each prompt; see run().
JEV_ENV_FILE = "/root/.xyne/agent/jev.env"

# Full call tracing (Jev payloads, LLM calls, tool outputs). The directory
# sits inside /logs/agent so harbor downloads it with the session logs.
SWE_TRACE_DIR_DEFAULT = "/logs/agent/swe-trace"


class XyneNativeCliAgent(BaseInstalledAgent):
    """Run xyne-cli's native kernel engine against grid.ai inside a harbor sandbox."""

    @staticmethod
    def name() -> str:
        return "xyne-cli-native"

    def _binary_dir(self) -> Path:
        return Path(os.environ.get("XYNE_NATIVE_BINARY_DIR", str(DEFAULT_BINARY_DIR)))

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

    def _jev_api_key(self) -> str | None:
        """Credential for Jev-guided reads; empty/absent leaves Jev off.

        JUSPAY_API_KEY wins when set; otherwise the main model's XYNE_API_KEY
        is reused, because the SystemOne endpoint and the completions endpoint
        share the same grid.ai.juspay.net auth realm.
        """
        return (
            self._get_env("JUSPAY_API_KEY")
            or self._get_env("XYNE_API_KEY")
            or None
        )

    def _jev_read_enabled(self) -> bool:
        """Jev runs only with a key and without an explicit opt-out.

        JEV_READ_SELECTOR=0 in the host environment forces the deterministic
        (non-Jev) read path even when a key is installed, so A/B runs can
        share every other setting.
        """
        return self._jev_api_key() is not None and (
            self._get_env("JEV_READ_SELECTOR") != "0"
        )

    def _swe_trace_enabled(self) -> bool:
        """Full call tracing follows Jev by default; SWE_TRACE=0 opts out.

        SWE_TRACE=1/true in the host environment forces tracing on even when
        Jev is off, so LLM/tool traces can be collected independently.
        """
        value = self._get_env("SWE_TRACE")
        if value == "0":
            return False
        if value == "1" or value.lower() == "true":
            return True
        return self._jev_read_enabled()

    def _swe_trace_dir(self) -> str:
        return self._get_env("SWE_TRACE_DIR") or SWE_TRACE_DIR_DEFAULT

    def get_version_command(self) -> str | None:
        return "xyne --version"

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Record the engine actually used, then fill harbor's token fields.

        `install()` symlinks the container's /root/.xyne/agent/sessions into
        /logs/agent/sessions, and harbor downloads the agent dir *before*
        calling this hook (`_download_agent_logs()` then
        `_populate_agent_context()` in harbor/trial/trial.py), so the
        transcripts are already on the host here under both the mounted and the
        DooD-unmounted transfer modes.

        The engine verdict comes from the session header, which the two engines
        write differently on purpose (`xyne-native-session` vs `session`). A
        verdict of `embedded` means the flag did not take and this trial
        measured the wrong engine — that is logged at error level and recorded
        on the context, but never raised: harbor is mid-finalization here and a
        raise would cost the graded result as well as the measurement. The
        install-time probe is the gate that can actually stop a run.

        Token totals come from the native `llm_usage` ledger — the native
        engine deliberately keeps usage OFF assistant messages, so the
        embedded reader would report zero here.

        Never raises: an accounting problem must not fail a graded trial.
        """
        sessions_dir = self.logs_dir / "sessions"

        try:
            engines = scan_engines(sessions_dir)
        except Exception:  # noqa: BLE001 — diagnostics must never fail a trial
            self.logger.exception("Failed to classify xyne session engines")
            engines = None

        if engines is not None:
            if engines["verdict"] != "native":
                self.logger.error(
                    "xyne-cli-native trial did not run on the native engine "
                    "(verdict=%s, native_files=%d, embedded_files=%d). "
                    "%s was set but the binary at %s did not honour it — its "
                    "results measure the WRONG engine.",
                    engines["verdict"],
                    engines["native_files"],
                    engines["embedded_files"],
                    NATIVE_ENV,
                    self._binary_dir(),
                )
            try:
                (self.logs_dir / "engine.json").write_text(
                    json.dumps(
                        {
                            "expected": ENGINE_NATIVE,
                            "profile": NATIVE_PROFILE,
                            **engines,
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            except OSError:
                self.logger.exception("Failed to write engine.json")

        try:
            totals = sum_session_usage(sessions_dir)
        except Exception:  # noqa: BLE001 — accounting must never fail a trial
            self.logger.exception("Failed to sum native xyne session usage")
            return

        if totals is None:
            self.logger.debug(
                "No native xyne session usage found under %s", self.logs_dir
            )
            return

        for field, value in to_harbor_fields(totals).items():
            setattr(context, field, value)
        context.metadata = {
            "usage_source": "xyne-native-llm-usage-ledger",
            "engine_expected": ENGINE_NATIVE,
            "engine_verdict": engines["verdict"] if engines else "unchecked",
            "native_profile": NATIVE_PROFILE,
            "session_files": totals["files"],
            "ledger_rows": totals["ledger_rows"],
            "fallback_messages": totals["fallback_messages"],
        }

    async def _detect_container_arch(self, environment: BaseEnvironment) -> str:
        result = await environment.exec(command="uname -m", user="root")
        machine = (result.stdout or "").strip()
        if machine in {"x86_64", "amd64"}:
            return "x64"
        if machine in {"aarch64", "arm64"}:
            return "arm64"
        raise RuntimeError(f"Unsupported container arch: {machine!r}")

    async def _assert_native_engine_available(
        self, environment: BaseEnvironment
    ) -> None:
        """Fail now if this binary ignores the native-harness flag.

        Costs one process spawn, zero tokens and zero network: the selector
        validates XYNE_NATIVE_PROFILE against its closed set during engine
        selection, long before a session is booted or a model is called. Run
        BEFORE models.json is written so a kernel-less binary cannot fall
        through to a real embedded turn.
        """
        result = await environment.exec(
            command=(
                f"{NATIVE_ENV} XYNE_NATIVE_PROFILE={PROBE_PROFILE} "
                "xyne prompt tb-native-probe --yolo "
                "2>&1 | tee /logs/agent/native-harness-probe.log; true"
            ),
            user="root",
        )
        output = (result.stdout or "") + (result.stderr or "")
        if PROBE_EXPECTED not in output:
            raise RuntimeError(
                "xyne binary does not support the native harness: probing with "
                f"XYNE_NATIVE_PROFILE={PROBE_PROFILE} did not produce "
                f"{PROBE_EXPECTED!r}, so {NATIVE_ENV} would be silently ignored "
                "and this run would measure the embedded-Pi engine. Rebuild "
                f"{self._binary_dir()} from the xyne-cli feat/native-harness "
                "branch (setup.sh does this). Probe output:\n"
                f"{output.strip()[:2000]}"
            )
        self.logger.info(
            "Native harness confirmed: binary honours %s (profile %s)",
            NATIVE_ENV,
            NATIVE_PROFILE,
        )

    async def install(self, environment: BaseEnvironment) -> None:
        binary_dir = self._binary_dir()
        package_json = binary_dir / "package.json"

        arch = await self._detect_container_arch(environment)
        binary = binary_dir / f"xyne-linux-{arch}"

        if not binary.exists():
            raise RuntimeError(
                f"native xyne linux binary not found at {binary}. setup.sh "
                "builds it from the xyne-cli feat/native-harness branch; see "
                "`setup_xyne_native_binary` there, or build it by hand with: "
                "cd xyne-cli && bun install --frozen-lockfile && bun run "
                "build:protocol && bun run build:compile && bun run "
                "build:webpack-bundle && bun run build:binary:prepare && bun "
                f"run build:binary:linux-{'arm' if arch == 'arm64' else 'x64'} "
                f"(then copy binaries/ and package.json into {binary_dir})."
            )
        if not package_json.exists():
            raise RuntimeError(
                f"package.json missing next to binary at {package_json}. "
                f"Copy xyne-cli's package.json into {binary_dir}."
            )

        # Redirect xyne's session transcripts into the bind-mounted /logs/agent
        # so harbor captures them even when the run is cancelled on timeout.
        # BOTH engines write under getProjectSessionsDir(), i.e.
        # <agent-dir>/sessions/<encoded-cwd>/, so symlinking `sessions` covers
        # the native path too. Only `sessions` is symlinked — models.json (API
        # key) stays out of /logs.
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

        # Before any credentials exist, so a kernel-less binary cannot answer
        # the probe with a real embedded turn.
        await self._assert_native_engine_available(environment)

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
        #
        # models.json is also load-bearing for the native path specifically:
        # the `standard` profile refuses to boot without it rather than mount a
        # fake provider (bootKernelRuntime in native-harness-selector.ts).
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

        jev_key = self._jev_api_key()
        if jev_key:
            # Shell-sourceable env file: the key reaches the container
            # without appearing in any command line (harbor's debug logger
            # and the tee'd /logs/agent/xyne.log capture commands, not file
            # contents). Written AFTER the native probe, like models.json,
            # so a kernel-less binary cannot use it.
            jev_lines = [f"JUSPAY_API_KEY={shlex.quote(jev_key)}"]
            for var in ("JEV_SYSTEMONE_ENDPOINT", "JEV_MODEL"):
                value = self._get_env(var)
                if value:
                    jev_lines.append(f"{var}={shlex.quote(value)}")
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".env", delete=False
            ) as tmp:
                tmp.write("\n".join(jev_lines) + "\n")
                jev_tmp = tmp.name
            try:
                await environment.upload_file(jev_tmp, JEV_ENV_FILE)
            finally:
                os.unlink(jev_tmp)
            await self.exec_as_root(
                environment, f"chmod 600 {JEV_ENV_FILE}"
            )
            self.logger.info(
                "Jev read selector armed (key installed at %s; "
                "JEV_READ_SELECTOR=1 will prefix every prompt)",
                JEV_ENV_FILE,
            )

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        escaped = shlex.quote(instruction)
        jev_enabled = self._jev_read_enabled()
        if jev_enabled:
            # Source the uploaded key file (never inline the value), then
            # arm the selector. The existence guard keeps run() safe even
            # if install() and run() disagree about the key's presence.
            jev_setup = (
                f"if [ -f {JEV_ENV_FILE} ]; then "
                f"set -a; . {JEV_ENV_FILE}; set +a; fi; "
            )
            jev_flag = " JEV_READ_SELECTOR=1"
        else:
            jev_setup = ""
            jev_flag = ""
        trace_flag = (
            f" SWE_TRACE=1 SWE_TRACE_DIR={shlex.quote(self._swe_trace_dir())}"
            if self._swe_trace_enabled()
            else ""
        )
        await self.exec_as_root(
            environment,
            # The env prefix IS the harness switch: session-factory.ts calls
            # selectEngine() on every runtime creation, and headless
            # `xyne prompt` goes through the same factory as the TUI, so the
            # kernel engine serves this turn. The leading echo puts the exact
            # flags in the same log as the agent output, so the dashboard log
            # viewer shows what was set without cross-referencing anything.
            #
            # --yolo is required, not a convenience. Headless `xyne prompt` has
            # no interactive approver, so without it every mutating tool call
            # (write/edit/bash) stalls at the permission gate and xyne exits 1
            # with "a tool call was not executed" — i.e. no terminal-bench task
            # can ever be solved. See handlePromptCommand in xyne-cli
            # src/core/services/cli-parser.ts. Do not pass --tools: when the
            # option is absent, xyne activates every tool registered in the
            # headless session; supplying it creates a strict allow-list that
            # can become stale as xyne's tool registry changes.
            command=(
                "{ "
                f"echo '[tb-native] {NATIVE_ENV} "
                f"XYNE_NATIVE_PROFILE={NATIVE_PROFILE}{jev_flag}{trace_flag}'; "
                f"{jev_setup}"
                f"{NATIVE_ENV} XYNE_NATIVE_PROFILE={NATIVE_PROFILE}"
                f"{jev_flag}{trace_flag} "
                f"xyne prompt {escaped} --yolo 2>&1; "
                "} | tee /logs/agent/xyne.log"
            ),
        )
