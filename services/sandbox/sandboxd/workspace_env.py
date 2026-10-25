"""The workspace ``.env`` file and the environment of processes started in a sandbox.

Upstream tools (the agentsociety2 CLI, the backend, ``ags.py`` and every skill
script) read configuration from ``<workspace>/.env``. The platform owns a marked
block at the top of that file and regenerates it on every sandbox start, so a
rotated project token or a changed default model takes effect without touching
anything the user added below the block.
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path

from dotenv import dotenv_values

from .config import Settings

BEGIN_MARKER = "# >>> platform-managed: regenerated on every sandbox start, edit via project settings >>>"
END_MARKER = "# <<< platform-managed <<<"

_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_./:@%+,=-]*$")

# Variables that must never leak from the agent's own environment into user processes.
_AGENT_ONLY_VARS = ("SANDBOX_AGENT_SECRET",)


def managed_values(settings: Settings) -> dict[str, str]:
    """Key/value pairs the platform controls in the workspace ``.env``."""
    token = settings.platform_token
    coder_model = settings.coder_model or settings.sim_model
    values: dict[str, str] = {
        "AGENTSOCIETY_LLM_API_KEY": token,
        "AGENTSOCIETY_LLM_API_BASE": settings.llm_base_url,
        "AGENTSOCIETY_LLM_MODEL": settings.sim_model,
        "AGENTSOCIETY_CODER_LLM_API_KEY": token,
        "AGENTSOCIETY_CODER_LLM_API_BASE": settings.llm_base_url,
        "AGENTSOCIETY_CODER_LLM_MODEL": coder_model,
        "AGENTSOCIETY_EMBEDDING_API_KEY": token,
        "AGENTSOCIETY_EMBEDDING_API_BASE": settings.llm_base_url,
        "AGENTSOCIETY_EMBEDDING_MODEL": settings.embedding_model,
        "AGENTSOCIETY_EMBEDDING_DIMS": str(settings.embedding_dims),
        "BACKEND_HOST": "127.0.0.1",
        "BACKEND_PORT": str(settings.backend_port),
        "WORKSPACE_PATH": str(settings.workspace),
        "PYTHON_PATH": settings.python,
        "LITERATURE_SEARCH_MCP_URL": settings.literature_mcp_url,
        "LITERATURE_SEARCH_API_KEY": token,
        "AGENTSOCIETY_DATASET_SERVER": settings.dataset_server,
        "AGENTSOCIETY_DATASET_TOKEN": token,
        "MEM0_TELEMETRY": "False",
        "ANONYMIZED_TELEMETRY": "False",
    }
    return {k: v for k, v in values.items() if v != ""}


def quote_env_value(value: str) -> str:
    if _SAFE_VALUE.match(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def render_managed_block(values: dict[str, str]) -> str:
    lines = [BEGIN_MARKER]
    lines += [f"{key}={quote_env_value(val)}" for key, val in values.items()]
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def split_user_lines(text: str) -> list[str]:
    """Lines of ``.env`` that are outside the managed block."""
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.strip() == BEGIN_MARKER:
            inside = True
            continue
        if line.strip() == END_MARKER:
            inside = False
            continue
        if not inside:
            out.append(line)
    return out


def write_workspace_env(settings: Settings, overrides: dict[str, str] | None = None) -> Path:
    """(Re)write the managed block, keeping user lines but dropping keys we now manage.

    ``overrides`` lets the platform pass per-project settings (for example a model
    picked in the UI) that win over the sandbox defaults.
    """
    env_path = settings.workspace / ".env"
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    values = managed_values(settings)
    if overrides:
        values.update({k: v for k, v in overrides.items() if v is not None})
    managed_keys = set(values)
    kept: list[str] = []
    for line in split_user_lines(existing):
        key = line.split("=", 1)[0].strip().removeprefix("export ").strip()
        if key and not key.startswith("#") and key in managed_keys:
            continue  # the managed block is authoritative for these keys
        kept.append(line)
    while kept and not kept[0].strip():
        kept.pop(0)
    content = render_managed_block(values)
    if kept:
        content += "\n" + "\n".join(kept).rstrip("\n") + "\n"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = env_path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(env_path)
    return env_path


def read_user_env(settings: Settings) -> dict[str, str]:
    """User-defined (non-managed) variables from the workspace ``.env``."""
    env_path = settings.workspace / ".env"
    if not env_path.exists():
        return {}
    user_text = "\n".join(split_user_lines(env_path.read_text(encoding="utf-8")))
    return {k: v for k, v in dotenv_values(stream=io.StringIO(user_text)).items() if v is not None}


def child_env(settings: Settings, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for processes started in the workspace (runs, backend, chat, IDE)."""
    env = {k: v for k, v in os.environ.items() if k not in _AGENT_ONLY_VARS}
    env_path = settings.workspace / ".env"
    if env_path.exists():
        env.update({k: v for k, v in dotenv_values(env_path).items() if v is not None})
    env["HOME"] = str(settings.home_dir)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env["AGENTSOCIETY_WORKSPACE"] = str(settings.workspace)
    env.update(settings.extra_child_env)
    if extra:
        env.update(extra)
    return env
