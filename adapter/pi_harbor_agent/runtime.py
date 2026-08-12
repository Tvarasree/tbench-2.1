"""Pure configuration and command builders for the Grid-backed Pi adapter."""
from __future__ import annotations

import shlex


PROVIDER = "juspay"
PI_PACKAGE = "@earendil-works/pi-coding-agent@latest"
NVM_INSTALL_VERSION = "v0.40.2"


def build_install_command() -> str:
    """Build a Harbor-version-independent Pi installation command."""
    return (
        "set -euo pipefail; "
        'NVM_DIR="${NVM_DIR:-$HOME/.nvm}"; export NVM_DIR; '
        'if [ ! -s "$NVM_DIR/nvm.sh" ]; then '
        'mkdir -p "$NVM_DIR"; '
        f"curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/"
        f"{NVM_INSTALL_VERSION}/install.sh | PROFILE=/dev/null bash; "
        "fi; "
        '. "$NVM_DIR/nvm.sh"; '
        "nvm install 22; nvm use 22; "
        f"npm install -g --ignore-scripts {shlex.quote(PI_PACKAGE)}; "
        "pi --version"
    )


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
        "--no-extensions --tools read,bash,edit,write,grep,find,ls "
        f"--provider {shlex.quote(PROVIDER)} --model {shlex.quote(model_id)} "
        f"< {shlex.quote(prompt_path)} 2>&1 | "
        f"stdbuf -oL tee {shlex.quote(output_path)}"
    )
