"""Authentication of control-plane calls and workspace-confined path handling."""

from __future__ import annotations

import hmac
import os
from pathlib import Path

from fastapi import Depends, HTTPException, Request, status

# Files the platform manages; users configure them through the platform API.
PROTECTED_FILES = frozenset({".env"})
# Never served or modified through the file API (VCS internals).
PROTECTED_DIRS = frozenset({".git"})


class PathOutsideWorkspace(HTTPException):
    def __init__(self, detail: str = "Path escapes the workspace") -> None:
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def require_agent_secret(request: Request) -> None:
    """FastAPI dependency: the caller must present the per-sandbox shared secret."""
    expected: str = request.app.state.settings.agent_secret
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        token = request.headers.get("x-sandbox-secret", "")
    if not token or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid sandbox secret")


AgentAuth = Depends(require_agent_secret)


def normalize_relative(rel: str) -> str:
    """Normalise a client-supplied workspace-relative path ('' means the root)."""
    if "\0" in rel:
        raise PathOutsideWorkspace("Invalid path")
    rel = rel.replace("\\", "/").strip()
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise PathOutsideWorkspace()
    return "/".join(parts)


def resolve_in_workspace(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root`` following symlinks, refusing anything outside it.

    Symlinks inside the workspace (``AGENTS.md -> CLAUDE.md``, ``.codex/skills``) are
    fine; a symlink that resolves outside the workspace is rejected so the file API
    can never be used to read the sandbox's own filesystem.
    """
    rel = normalize_relative(rel)
    root_resolved = Path(os.path.realpath(root))
    candidate = Path(os.path.realpath(root_resolved / rel)) if rel else root_resolved
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise PathOutsideWorkspace()
    return candidate


def relative_to_workspace(root: Path, path: Path) -> str:
    root_resolved = Path(os.path.realpath(root))
    try:
        return path.relative_to(root_resolved).as_posix()
    except ValueError:
        return Path(os.path.realpath(path)).relative_to(root_resolved).as_posix()


def is_protected(rel: str) -> bool:
    rel = normalize_relative(rel)
    if not rel:
        return False
    first = rel.split("/", 1)[0]
    return rel in PROTECTED_FILES or first in PROTECTED_DIRS


def ensure_not_protected(rel: str) -> None:
    if is_protected(rel):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"'{normalize_relative(rel)}' is managed by the platform",
        )
