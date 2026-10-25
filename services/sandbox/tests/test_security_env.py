from __future__ import annotations

import os

import pytest
from fastapi import HTTPException

from sandboxd.security import is_protected, normalize_relative, resolve_in_workspace
from sandboxd.workspace_env import BEGIN_MARKER, END_MARKER, child_env, read_user_env, write_workspace_env


def test_normalize_rejects_traversal():
    assert normalize_relative("a/./b//c/") == "a/b/c"
    assert normalize_relative("") == ""
    with pytest.raises(HTTPException):
        normalize_relative("../etc/passwd")
    with pytest.raises(HTTPException):
        normalize_relative("a/../../b")
    with pytest.raises(HTTPException):
        normalize_relative("bad\0name")


def test_resolve_blocks_symlink_escape(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "inside.txt").write_text("ok")
    (root / "link-in").symlink_to("inside.txt")
    (root / "link-out").symlink_to("/etc")
    assert resolve_in_workspace(root, "link-in").name == "inside.txt"
    with pytest.raises(HTTPException):
        resolve_in_workspace(root, "link-out/passwd")
    with pytest.raises(HTTPException):
        resolve_in_workspace(root, "link-out")


def test_protected_paths():
    assert is_protected(".env")
    assert is_protected(".git/config")
    assert not is_protected("papers/.env.example")
    assert not is_protected("custom/agents/a.py")


def test_env_block_regenerates_and_keeps_user_lines(make_settings):
    settings = make_settings()
    env_path = settings.workspace / ".env"
    env_path.write_text("MY_VAR=1\nAGENTSOCIETY_LLM_MODEL=user-model\n# comment\n")
    write_workspace_env(settings)
    text = env_path.read_text()
    assert text.startswith(BEGIN_MARKER)
    assert END_MARKER in text
    assert "MY_VAR=1" in text and "# comment" in text
    # a key the platform manages is not duplicated in the user section
    assert text.count("AGENTSOCIETY_LLM_MODEL=") == 1
    assert "AGENTSOCIETY_LLM_API_KEY=sbx_project_token" in text
    assert oct(env_path.stat().st_mode & 0o777) == "0o600"

    # rotating the token rewrites only the managed block
    rotated = make_settings(platform_token="sbx_rotated")
    write_workspace_env(rotated, {"AGENTSOCIETY_LLM_MODEL": "deepseek-v4"})
    text = env_path.read_text()
    assert "sbx_project_token" not in text and "sbx_rotated" in text
    assert "AGENTSOCIETY_LLM_MODEL=deepseek-v4" in text and "MY_VAR=1" in text
    assert read_user_env(rotated) == {"MY_VAR": "1"}


def test_child_env_never_leaks_agent_secret(make_settings, monkeypatch):
    settings = make_settings()
    monkeypatch.setenv("SANDBOX_AGENT_SECRET", "do-not-leak")
    write_workspace_env(settings)
    env = child_env(settings)
    assert "SANDBOX_AGENT_SECRET" not in env
    assert env["AGENTSOCIETY_LLM_API_BASE"] == settings.llm_base_url
    assert env["HOME"] == str(settings.home_dir)
    assert os.path.isabs(env["AGENTSOCIETY_WORKSPACE"])
