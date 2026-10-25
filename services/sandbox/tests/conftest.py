from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

os.environ.setdefault("MEM0_TELEMETRY", "False")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

FAKE_CLAUDE = textwrap.dedent(
    """\
    #!{python}
    # Stand-in for the Claude Code CLI: speaks the stream-json protocol.
    import json, os, sys
    args = sys.argv[1:]
    prompt = sys.stdin.read()
    model = args[args.index("--model") + 1]
    sid = args[args.index("--resume") + 1] if "--resume" in args else "sess-fake-1"
    def out(obj):
        print(json.dumps(obj), flush=True)
    out({{"type": "system", "subtype": "init", "session_id": sid, "model": model, "cwd": os.getcwd()}})
    if prompt.strip() == "fail":
        print("boom from fake claude", file=sys.stderr)
        sys.exit(3)
    out({{"type": "assistant", "session_id": sid, "message": {{"role": "assistant", "content": [{{"type": "text", "text": "echo: " + prompt}}]}}}})
    out({{"type": "result", "subtype": "success", "is_error": False, "result": "echo: " + prompt,
         "session_id": sid, "num_turns": 1, "duration_ms": 5,
         "usage": {{"input_tokens": 3, "output_tokens": 2}},
         "seen_env": {{k: os.environ.get(k) for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "SANDBOX_AGENT_SECRET", "IS_SANDBOX")}},
         "resumed": "--resume" in args, "permission_mode": args[args.index("--permission-mode") + 1]}})
    """
)


@pytest.fixture(scope="session")
def assets_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("assets") / "extension"
    subprocess.run([str(REPO / "scripts" / "prepare_sandbox_assets.sh"), str(out)], check=True, capture_output=True)
    return out


@pytest.fixture
def fake_claude(tmp_path) -> Path:
    path = tmp_path / "fake-claude"
    path.write_text(FAKE_CLAUDE.format(python=sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def make_settings(tmp_path, assets_dir, fake_claude):
    from sandboxd.config import Settings

    def factory(**overrides) -> Settings:
        values = dict(
            workspace=tmp_path / "data" / "workspace",
            state_dir=tmp_path / "data" / "state",
            agent_secret="test-secret",
            python=sys.executable,
            assets_dir=assets_dir,
            enable_backend=False,
            claude_bin=str(fake_claude),
            platform_token="sbx_project_token",
            llm_base_url="http://127.0.0.1:9/llm/v1",
            anthropic_base_url="http://127.0.0.1:9/llm/anthropic",
            literature_mcp_url="http://127.0.0.1:9/literature/mcp/",
            dataset_server="http://127.0.0.1:9",
            topic="Echo chambers",
        )
        values.update(overrides)
        settings = Settings(**values)
        settings.workspace.mkdir(parents=True, exist_ok=True)
        return settings

    return factory


AUTH = {"Authorization": "Bearer test-secret"}
