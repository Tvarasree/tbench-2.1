"""Pure configuration and command builders for the Grid-backed Pi adapter."""
from __future__ import annotations

import shlex


PROVIDER = "juspay"
PI_PACKAGE = "@earendil-works/pi-coding-agent@latest"


def build_models_config(
    *, api_key: str, base_url: str, model_id: str
) -> dict[str, object]:
    return {
        "providers": {
            PROVIDER: {
                "baseUrl": base_url.rstrip("/"),
                "api": "openai-completions",
                "apiKey": api_key,
                "models": [{"id": model_id, "name": model_id}],
            }
        }
    }


def build_settings(model_id: str) -> dict[str, str]:
    return {"defaultProvider": PROVIDER, "defaultModel": model_id}


def build_run_command(
    *, model_id: str, prompt_path: str, output_path: str
) -> str:
    return (
        "set -o pipefail; . ~/.nvm/nvm.sh; "
        "pi --print --mode json --no-session "
        f"--provider {shlex.quote(PROVIDER)} --model {shlex.quote(model_id)} "
        f"< {shlex.quote(prompt_path)} 2>&1 | "
        f"stdbuf -oL tee {shlex.quote(output_path)}"
    )
