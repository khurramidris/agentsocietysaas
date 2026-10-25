"""Settings for the sandbox agent, read once from the environment.

The orchestrator sets these variables when it creates the sandbox. Everything a
sandbox needs to reach the platform (LLM gateway, literature search, dataset
hub) is addressed through PLATFORM_* URLs and authenticated with a single
project-scoped PLATFORM_TOKEN; no provider keys or other platform secrets are
ever handed to a sandbox.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    workspace: Path
    state_dir: Path
    agent_secret: str
    host: str = "0.0.0.0"
    port: int = 7000

    project_id: str = ""
    project_name: str = ""
    topic: str = ""

    # Interpreter that has agentsociety2 installed (experiments, backend, skills).
    python: str = sys.executable
    # Patched copy of upstream extension assets: skills/, runtime/, plugins/.
    assets_dir: Path = Path("/opt/agentsociety/extension")

    enable_backend: bool = True
    backend_port: int = 8001

    ide_enabled: bool = False
    ide_port: int = 8080
    code_server_bin: str = "code-server"

    claude_bin: str = "claude"
    chat_model: str = "claude-opus-5-5"
    chat_small_model: str = "claude-haiku-5-5"

    platform_token: str = ""
    llm_base_url: str = ""
    anthropic_base_url: str = ""
    literature_mcp_url: str = ""
    dataset_server: str = ""

    sim_model: str = "gpt-5.5"
    coder_model: str = ""
    embedding_model: str = "text-embedding-3-large"
    embedding_dims: int = 1024

    max_text_file_bytes: int = 2 * 1024 * 1024
    max_upload_bytes: int = 512 * 1024 * 1024
    extra_child_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> Settings:
        secret = _env("SANDBOX_AGENT_SECRET")
        if not secret:
            raise RuntimeError("SANDBOX_AGENT_SECRET must be set")
        workspace = Path(_env("SANDBOX_WORKSPACE", "/data/workspace")).expanduser()
        state_dir = Path(_env("SANDBOX_STATE_DIR", str(workspace.parent / "state"))).expanduser()
        return cls(
            workspace=workspace,
            state_dir=state_dir,
            agent_secret=secret,
            host=_env("SANDBOX_HOST", "0.0.0.0"),
            port=_env_int("SANDBOX_PORT", 7000),
            project_id=_env("SANDBOX_PROJECT_ID"),
            project_name=_env("SANDBOX_PROJECT_NAME"),
            topic=_env("SANDBOX_TOPIC"),
            python=_env("SANDBOX_PYTHON", sys.executable),
            assets_dir=Path(_env("SANDBOX_ASSETS_DIR", "/opt/agentsociety/extension")),
            enable_backend=_env_bool("SANDBOX_ENABLE_BACKEND", True),
            backend_port=_env_int("SANDBOX_BACKEND_PORT", 8001),
            ide_enabled=_env_bool("SANDBOX_ENABLE_IDE", False),
            ide_port=_env_int("SANDBOX_IDE_PORT", 8080),
            code_server_bin=_env("SANDBOX_CODE_SERVER_BIN", "code-server"),
            claude_bin=_env("SANDBOX_CLAUDE_BIN", shutil.which("claude") or "claude"),
            chat_model=_env("SANDBOX_CHAT_MODEL", "claude-opus-5-5"),
            chat_small_model=_env("SANDBOX_CHAT_SMALL_MODEL", "claude-haiku-5-5"),
            platform_token=_env("PLATFORM_TOKEN"),
            llm_base_url=_env("PLATFORM_LLM_BASE_URL"),
            anthropic_base_url=_env("PLATFORM_ANTHROPIC_BASE_URL"),
            literature_mcp_url=_env("PLATFORM_LITERATURE_MCP_URL"),
            dataset_server=_env("PLATFORM_DATASET_SERVER"),
            sim_model=_env("SANDBOX_SIM_MODEL", "gpt-5.5"),
            coder_model=_env("SANDBOX_CODER_MODEL"),
            embedding_model=_env("SANDBOX_EMBEDDING_MODEL", "text-embedding-3-large"),
            embedding_dims=_env_int("SANDBOX_EMBEDDING_DIMS", 1024),
            max_text_file_bytes=_env_int("SANDBOX_MAX_TEXT_FILE_BYTES", 2 * 1024 * 1024),
            max_upload_bytes=_env_int("SANDBOX_MAX_UPLOAD_BYTES", 512 * 1024 * 1024),
        )

    @property
    def home_dir(self) -> Path:
        """HOME for child processes; kept on the persistent volume next to the workspace."""
        return self.state_dir / "home"

    @property
    def chat_dir(self) -> Path:
        return self.state_dir / "chat"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"
