"""Bootstrap against the real agentsociety2 package (slow: imports the engine)."""

from __future__ import annotations

import ast
import json
import subprocess
import sys

from sandboxd.bootstrap import bootstrap, read_marker
from sandboxd.routers.tools import ALLOWED_TOOLS

from .conftest import REPO


def test_full_bootstrap_matches_upstream_layout(make_settings):
    settings = make_settings()
    result = bootstrap(settings)
    ws = settings.workspace
    assert result.initialized and result.skills_synced, result.warnings
    for rel in (
        "TOPIC.md",
        "CLAUDE.md",
        "AGENTS.md",
        ".gitignore",
        ".env",
        "papers/literature_index.json",
        ".agentsociety/progress.json",
        ".agentsociety/prefill_params.json",
        ".agentsociety/path.md",
        ".agentsociety/bin/ags.py",
        ".claude/settings.json",
        "custom/agents",
        "custom/envs",
        "user_data",
        "datasets",
    ):
        assert (ws / rel).exists(), rel
    assert (ws / "AGENTS.md").is_symlink()
    assert (ws / ".codex" / "skills").is_symlink()
    assert "Hosted Platform Notes" in (ws / "CLAUDE.md").read_text()
    progress = json.loads((ws / ".agentsociety/progress.json").read_text())
    assert progress["workspace"]["topic"] == "Echo chambers"
    assert progress["workspace"]["current_stage"] == "literature_search"
    skills = {p.name for p in (ws / ".claude/skills").iterdir()}
    assert "agentsociety-research-pipeline" in skills
    assert (ws / ".claude/skills/agentsociety-research-pipeline/SKILL.md").exists()
    assert not skills & {"pdf", "docx", "xlsx", "pptx"}
    assert list((ws / ".agentsociety/agent_classes").glob("*.json"))
    log = subprocess.run(["git", "-C", str(ws), "log", "--oneline"], capture_output=True, text=True).stdout
    assert "init: workspace initialized" in log

    # the patched dataset scripts are what got synced
    use_py = (ws / ".claude/skills/agentsociety-use-dataset/scripts/use.py").read_text()
    assert "AGENTSOCIETY_DATASET_SERVER" in use_py

    # upstream launcher works in the new workspace
    out = subprocess.run(
        [sys.executable, ".agentsociety/bin/ags.py", "research-pipeline", "where-am-i", "--json"],
        cwd=ws,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["current_stage"] == "literature_search"

    # second run is a no-op, and a changed asset bundle triggers a re-sync
    again = bootstrap(settings)
    assert not again.initialized and not again.skills_synced
    marker = read_marker(settings)
    assert marker["assets_fingerprint"]
    (settings.assets_dir / "skills" / "agentsociety-hypothesis" / "v1.0.0" / "EXTRA.md").write_text("x")
    try:
        assert bootstrap(settings).skills_synced
        assert (ws / ".claude/skills/agentsociety-hypothesis/EXTRA.md").exists()
    finally:
        (settings.assets_dir / "skills" / "agentsociety-hypothesis" / "v1.0.0" / "EXTRA.md").unlink()


def test_tool_allowlist_matches_upstream_launcher():
    source = (REPO / "upstream/agentsociety/extension/runtime/agentsociety/bin/ags.py").read_text()
    tree = ast.parse(source)
    upstream_tools = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "TOOL_SCRIPTS" for t in node.targets):
            upstream_tools = {k.value for k in node.value.keys}
    assert upstream_tools == set(ALLOWED_TOOLS)
