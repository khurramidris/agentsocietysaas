"""Workspace bootstrap for a hosted project.

This is a Python port of the AI Social Scientist extension's "Initialize Research
Project" command (``extension/src/workspaceManager.ts`` in AgentSociety,
Apache-2.0) so a hosted workspace has exactly the layout, state files, skills and
launcher a locally initialised one has. Differences from upstream:

* the Office document skills (pdf/docx/xlsx/pptx) are not copied: upstream ships
  them under a proprietary license, see upstream/UPSTREAM.md;
* ``.env`` is written by :mod:`sandboxd.workspace_env` (platform-managed block);
* CLAUDE.md gets a short "Hosted Platform Notes" section appended;
* skills are copied (not symlinked into an extension install dir) and re-synced
  whenever the bundled asset set changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings
from .workspace_env import child_env, write_workspace_env

log = logging.getLogger("sandboxd.bootstrap")

TEMPLATES = Path(__file__).parent / "templates"
MARKER = "workspace-marker.json"  # in state_dir: upstream init prunes unknown .agentsociety/*.json
OVERRIDES = "workspace-overrides.json"

GITIGNORE_ENTRIES = [
    ".env",
    ".claude/*",
    ".claude/skills/",
    ".claude/settings.local.json",
    "!.claude/settings.json",
    ".codex/*",
    ".codex/skills/",
    "# Python cache files in custom/",
    "custom/**/__pycache__/",
    "custom/**/*.pyc",
    "custom/**/.pytest_cache/",
]

# Upstream bundles these; they are proprietary and deliberately absent here.
EXCLUDED_SKILLS = frozenset({"pdf", "docx", "xlsx", "pptx"})
VERSIONED_PREFIX = "agentsociety-"
GIT_IDENTITY = ("AI Social Scientist", "workspace@sandbox.invalid")


@dataclass
class BootstrapResult:
    initialized: bool = False
    skills_synced: bool = False
    created: list[str] = field(default_factory=list)
    synced: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "initialized": self.initialized,
            "skills_synced": self.skills_synced,
            "created": self.created,
            "synced": self.synced,
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------- helpers


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _git(settings: Settings, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    env = child_env(settings)
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=settings.workspace,
        env=env,
        capture_output=True,
        text=True,
        check=check,
    )


def _ensure_git_repo(settings: Settings, result: BootstrapResult) -> bool:
    probe = _git(settings, "rev-parse", "--show-toplevel")
    if probe.returncode == 0 and Path(probe.stdout.strip()).resolve() == settings.workspace.resolve():
        return False
    init = _git(settings, "init", "--initial-branch=main")
    if init.returncode != 0:
        init = _git(settings, "init")
    if init.returncode != 0:
        result.warnings.append(f"git init failed: {init.stderr.strip()}")
        return False
    result.created.append(".git/")
    return True


def _ensure_git_identity(settings: Settings) -> None:
    name = _git(settings, "config", "user.name").stdout.strip()
    if not name:
        _git(settings, "config", "user.name", GIT_IDENTITY[0])
        _git(settings, "config", "user.email", GIT_IDENTITY[1])


def auto_commit(settings: Settings, message: str) -> bool:
    """``git add -A && git commit`` when something is staged (upstream autoCommit)."""
    if _git(settings, "rev-parse", "--git-dir").returncode != 0:
        return False
    _ensure_git_identity(settings)
    if _git(settings, "add", "-A").returncode != 0:
        return False
    if _git(settings, "diff", "--cached", "--quiet").returncode == 0:
        return False
    return _git(settings, "commit", "-m", message).returncode == 0


def _update_gitignore(path: Path) -> None:
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = [line for line in content.split("\n") if line.strip() != ".claude/"]
    modified = len(lines) != len(content.split("\n"))
    content = "\n".join(lines)
    existing = {line.strip() for line in lines if line.strip() and not line.strip().startswith("#")}
    for entry in GITIGNORE_ENTRIES:
        if entry not in existing:
            if content and not content.endswith("\n"):
                content += "\n"
            content += entry + "\n"
            modified = True
    if modified or not path.exists():
        path.write_text(content, encoding="utf-8")


def _write_if_missing(path: Path, text: str, result: BootstrapResult, rel: str) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    result.created.append(rel)


def _write_json_if_missing(path: Path, payload: object, result: BootstrapResult, rel: str) -> None:
    _write_if_missing(path, json.dumps(payload, indent=2, ensure_ascii=False), result, rel)


def _empty_stage() -> dict:
    return {
        "status": "not_started",
        "started_at": None,
        "completed_at": None,
        "attempts": 0,
        "error": None,
        "metadata": {},
        "verification_status": "not_started",
        "revision_round": 0,
    }


def _initial_progress(topic: str) -> dict:
    return {
        "version": "1.3",
        "workspace": {
            "topic": topic or "",
            "created_at": _now(),
            "current_stage": "literature_search",
            "current_hypothesis_id": None,
            "current_experiment_id": None,
            "revision_round": 0,
            "active_reroute": None,
        },
        "stages": {
            stage: _empty_stage()
            for stage in (
                "literature_search",
                "hypothesis",
                "experiment_config",
                "run_experiment",
                "analysis",
                "generate_paper",
            )
        },
        "hypotheses": {},
        "transitions": [],
    }


def _python_env(settings: Settings) -> dict[str, str]:
    env = child_env(settings)
    # agentsociety2 validates these at import time even for commands that never
    # call an LLM (module discovery, custom templates).
    env.setdefault("AGENTSOCIETY_LLM_API_KEY", "not-configured")
    env.setdefault("AGENTSOCIETY_LLM_API_BASE", "http://127.0.0.1:9/v1")
    if not env.get("AGENTSOCIETY_LLM_API_KEY"):
        env["AGENTSOCIETY_LLM_API_KEY"] = "not-configured"
    return env


def _init_custom_modules(settings: Settings, result: BootstrapResult) -> None:
    if (settings.workspace / "custom").exists():
        return
    proc = subprocess.run(
        [settings.python, "-m", "agentsociety2.society.workspace", "init-custom",
         "--target-dir", str(settings.workspace), "--json"],
        cwd=settings.workspace,
        env=_python_env(settings),
        capture_output=True,
        text=True,
        timeout=300,
    )
    try:
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if proc.returncode == 0 and payload.get("success"):
        result.created.extend(payload.get("created", []))
        return
    # Upstream falls back to bare directories when the CLI is unavailable.
    for sub in ("agents", "envs", "skills"):
        (settings.workspace / "custom" / sub).mkdir(parents=True, exist_ok=True)
    result.created.append("custom/ (fallback)")
    result.warnings.append(
        "agentsociety2 init-custom failed; created empty custom/ directories: "
        + (proc.stderr.strip()[-500:] or "no output")
    )


_MODULE_INFO_SCRIPT = """
import json, sys
from pathlib import Path
from agentsociety2.society.workspace import create_module_info_files
print(json.dumps(create_module_info_files(Path(sys.argv[1]))))
"""


def refresh_module_info(settings: Settings) -> dict:
    """Write ``.agentsociety/{agent_classes,env_modules}/*.json`` for built-in modules."""
    proc = subprocess.run(
        [settings.python, "-c", _MODULE_INFO_SCRIPT, str(settings.workspace)],
        cwd=settings.workspace,
        env=_python_env(settings),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        return {"success": False, "error": proc.stderr.strip()[-2000:]}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"success": False, "error": "unparseable output"}


def _write_state_files(settings: Settings, topic: str, result: BootstrapResult) -> None:
    ws = settings.workspace
    agentsociety = ws / ".agentsociety"
    for sub in ("agent_classes", "env_modules", "data", "custom_env_skill/runs"):
        (agentsociety / sub).mkdir(parents=True, exist_ok=True)
    _write_if_missing(
        agentsociety / "path.md",
        (TEMPLATES / "workspace_path.md.tmpl").read_text(encoding="utf-8"),
        result,
        ".agentsociety/path.md",
    )
    _write_json_if_missing(
        agentsociety / "prefill_params.json",
        {"version": "1.0", "env_modules": {}, "agents": {}},
        result,
        ".agentsociety/prefill_params.json",
    )
    _write_json_if_missing(
        agentsociety / "progress.json", _initial_progress(topic), result, ".agentsociety/progress.json"
    )


def _write_claude_md(settings: Settings, result: BootstrapResult) -> None:
    ws = settings.workspace
    claude_md = ws / "CLAUDE.md"
    if not claude_md.exists():
        text = (TEMPLATES / "workspace_claude.md.tmpl").read_text(encoding="utf-8")
        text = text.rstrip("\n") + "\n" + (TEMPLATES / "workspace_hosted_notes.md.tmpl").read_text(encoding="utf-8")
        claude_md.write_text(text, encoding="utf-8")
        result.created.append("CLAUDE.md")
    agents_md = ws / "AGENTS.md"
    if not agents_md.exists() and not agents_md.is_symlink():
        try:
            agents_md.symlink_to("CLAUDE.md")
            result.created.append("AGENTS.md (symlink to CLAUDE.md)")
        except OSError:
            shutil.copyfile(claude_md, agents_md)
            result.created.append("AGENTS.md (copy of CLAUDE.md)")


def _seed_claude_settings(settings: Settings) -> bool:
    """Merge the paper-toolkit plugin marketplace into ``.claude/settings.json`` (upstream)."""
    path = settings.workspace / ".claude" / "settings.json"
    data: dict = {}
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return False
    desired = {"source": {"source": "github", "repo": "Yokumii/paper-toolkit"}}
    marketplaces = data.get("extraKnownMarketplaces") or {}
    enabled = data.get("enabledPlugins") or {}
    if marketplaces.get("paper-toolkit") == desired and enabled.get("paper-toolkit@paper-toolkit") is True:
        return False
    marketplaces["paper-toolkit"] = desired
    enabled["paper-toolkit@paper-toolkit"] = True
    data["extraKnownMarketplaces"] = marketplaces
    data["enabledPlugins"] = enabled
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


# --------------------------------------------------------------------------- assets


def _copytree(src: Path, dst: Path) -> None:
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def assets_fingerprint(assets_dir: Path) -> str:
    """Content hash of the bundled skills/runtime/plugins, used to decide re-syncs."""
    digest = hashlib.sha256()
    for sub in ("skills", "runtime", "plugins"):
        base = assets_dir / sub
        if not base.exists():
            continue
        for path in sorted(p for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            digest.update(path.relative_to(assets_dir).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _skill_default_version(skill_dir: Path) -> Path | None:
    manifest = skill_dir / "manifest.json"
    if not manifest.exists():
        return None
    try:
        version = json.loads(manifest.read_text(encoding="utf-8")).get("defaultVersion")
    except json.JSONDecodeError:
        return None
    candidate = skill_dir / str(version)
    return candidate if version and candidate.is_dir() else None


def sync_assets(settings: Settings, result: BootstrapResult) -> None:
    """Copy the launcher, skills and plugin skills/commands into the workspace."""
    ws = settings.workspace
    assets = settings.assets_dir
    runtime_bin = assets / "runtime" / "agentsociety" / "bin"
    if not runtime_bin.is_dir():
        raise FileNotFoundError(f"bundled runtime not found: {runtime_bin}")
    target_bin = ws / ".agentsociety" / "bin"
    _copytree(runtime_bin, target_bin)
    result.synced.append(".agentsociety/bin/ags.py")

    skills_src = assets / "skills"
    skills_dst = ws / ".claude" / "skills"
    skills_dst.mkdir(parents=True, exist_ok=True)
    for item in sorted(skills_src.iterdir()):
        if item.name in EXCLUDED_SKILLS or not item.is_dir():
            continue
        if item.name.startswith(VERSIONED_PREFIX):
            version_dir = _skill_default_version(item)
            if version_dir is None:
                result.warnings.append(f"skill {item.name} has no usable defaultVersion")
                continue
            _copytree(version_dir, skills_dst / item.name)
        else:
            _copytree(item, skills_dst / item.name)
        result.synced.append(item.name)

    # Upstream moved analysis support bundles under agentsociety-analysis/support/.
    for legacy in (ws / ".agentsociety" / "analysis-support", skills_dst / "frontend-design"):
        if legacy.exists():
            shutil.rmtree(legacy, ignore_errors=True)

    codex_dir = ws / ".codex"
    codex_dir.mkdir(exist_ok=True)
    codex_skills = codex_dir / "skills"
    if not codex_skills.exists() and not codex_skills.is_symlink():
        try:
            codex_skills.symlink_to(os.path.relpath(skills_dst, codex_dir), target_is_directory=True)
            result.created.append(".codex/skills (symlink to ../.claude/skills)")
        except OSError as exc:
            result.warnings.append(f"could not link .codex/skills: {exc}")

    plugins = assets / "plugins"
    if plugins.is_dir():
        commands_dst = ws / ".claude" / "commands"
        for plugin in sorted(p for p in plugins.iterdir() if p.is_dir()):
            for skill in sorted(p for p in (plugin / "skills").glob("*") if p.is_dir()):
                _copytree(skill, skills_dst / skill.name)
                result.synced.append(skill.name)
            commands = sorted((plugin / "commands").glob("*.md"))
            if commands:
                commands_dst.mkdir(parents=True, exist_ok=True)
            for cmd in commands:
                shutil.copy2(cmd, commands_dst / cmd.name)
                result.synced.append(cmd.name)


# --------------------------------------------------------------------------- entry points


def read_marker(settings: Settings) -> dict:
    path = settings.state_dir / MARKER
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def read_overrides(settings: Settings) -> dict[str, str]:
    """Per-project ``.env`` overrides pushed by the control plane (see /workspace/settings)."""
    path = settings.state_dir / OVERRIDES
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {str(k): str(v) for k, v in data.items() if v is not None}


def write_overrides(settings: Settings, overrides: dict[str, str]) -> None:
    path = settings.state_dir / OVERRIDES
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(overrides, indent=2, sort_keys=True), encoding="utf-8")


def bootstrap(settings: Settings, *, topic: str | None = None, force_sync: bool = False) -> BootstrapResult:
    """Idempotently prepare the workspace. Safe to call on every sandbox start."""
    result = BootstrapResult()
    ws = settings.workspace
    for d in (ws, settings.state_dir, settings.home_dir, settings.chat_dir, settings.logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    write_workspace_env(settings, read_overrides(settings))
    marker = read_marker(settings)
    topic = topic if topic is not None else settings.topic

    if not marker.get("initialized_at"):
        created_repo = _ensure_git_repo(settings, result)
        if created_repo:
            _ensure_git_identity(settings)
            _git(settings, "add", "-A")
            _git(settings, "commit", "-m", "init: bootstrap research workspace", "--allow-empty")
        _update_gitignore(ws / ".gitignore")
        _write_if_missing(ws / "TOPIC.md", f"# {topic or settings.project_name or 'Research topic'}\n\n", result, "TOPIC.md")
        for sub in ("papers", "user_data", "datasets"):
            if not (ws / sub).exists():
                (ws / sub).mkdir(parents=True)
                result.created.append(f"{sub}/")
        _init_custom_modules(settings, result)
        _write_json_if_missing(
            ws / "papers" / "literature_index.json",
            {"version": "1.0", "created_at": _now(), "updated_at": _now(), "entries": []},
            result,
            "papers/literature_index.json",
        )
        _write_state_files(settings, topic or "", result)
        _write_claude_md(settings, result)
        info = refresh_module_info(settings)
        if not info.get("success"):
            result.warnings.append(f"module info generation failed: {info.get('error', info.get('message'))}")
        result.initialized = True

    fingerprint = assets_fingerprint(settings.assets_dir)
    if force_sync or result.initialized or marker.get("assets_fingerprint") != fingerprint:
        sync_assets(settings, result)
        result.skills_synced = True
    if _seed_claude_settings(settings):
        result.created.append(".claude/settings.json")

    marker.update(
        {
            "initialized_at": marker.get("initialized_at") or _now(),
            "assets_fingerprint": fingerprint,
            "last_bootstrap_at": _now(),
            "project_id": settings.project_id,
        }
    )
    (settings.state_dir / MARKER).write_text(json.dumps(marker, indent=2), encoding="utf-8")

    if result.initialized:
        auto_commit(settings, "init: workspace initialized")
    elif result.skills_synced:
        auto_commit(settings, f"sync: platform skills ({len(result.synced)} items)")
    log.info("bootstrap done: %s", result.as_dict())
    return result
