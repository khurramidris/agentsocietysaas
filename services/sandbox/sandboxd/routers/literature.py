"""Workspace literature library operations.

The extension manages ``papers/literature_index.json`` by running two upstream
module CLIs in the workspace (extension/src/services/literatureIngest.ts):
``agentsociety2.skills.literature.ingest`` (add by DOI/arXiv id, register a local
PDF/Markdown file) and ``agentsociety2.skills.literature.library_ops`` (fill
metadata and fetch open-access PDFs, download PDFs, export/import BibTeX). This
router runs the same commands with the same arguments.
"""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..security import AgentAuth, resolve_in_workspace
from ..workspace_env import child_env

router = APIRouter(prefix="/literature", tags=["literature"], dependencies=[AgentAuth])

Op = Literal["add-id", "add-file", "sync", "download-pdf", "export-bib", "import-bib"]
INGEST_OPS = {"add-id", "add-file"}
TIMEOUTS = {"add-id": 60.0, "add-file": 60.0, "sync": 300.0, "download-pdf": 300.0, "export-bib": 60.0, "import-bib": 120.0}


class LibraryOp(BaseModel):
    op: Op
    identifier: str | None = Field(None, max_length=500, description="DOI, doi.org URL or arXiv id/URL (add-id)")
    file: str | None = Field(None, max_length=1000, description="Workspace-relative PDF/Markdown (add-file) or .bib (import-bib)")
    title: str | None = Field(None, max_length=1000)
    entries: list[int] | None = Field(None, description="0-based entry ids (sync, download-pdf, export-bib)")
    no_pdf: bool = False
    force: bool = False
    stdout_only: bool = False


def build_args(body: LibraryOp, workspace) -> tuple[str, list[str]]:
    module = "agentsociety2.skills.literature." + ("ingest" if body.op in INGEST_OPS else "library_ops")
    args = [body.op]
    if body.op == "add-id":
        if not body.identifier:
            raise HTTPException(400, "identifier is required")
        args.append(body.identifier.strip())
    elif body.op in ("add-file", "import-bib"):
        if not body.file:
            raise HTTPException(400, "file is required")
        target = resolve_in_workspace(workspace, body.file)
        if not target.is_file():
            raise HTTPException(404, f"Not a file: {body.file}")
        args.append(str(target))
        if body.op == "add-file" and body.title:
            args += ["--title", body.title]
    if body.entries and body.op in ("sync", "download-pdf", "export-bib"):
        args += ["--entries", ",".join(str(int(e)) for e in body.entries)]
    if body.op == "sync" and body.no_pdf:
        args.append("--no-pdf")
    if body.op == "download-pdf" and body.force:
        args.append("--force")
    if body.op == "export-bib" and body.stdout_only:
        args.append("--stdout-only")
    return module, args


@router.post("/library")
async def library_op(request: Request, body: LibraryOp):
    settings = request.app.state.settings
    module, args = build_args(body, settings.workspace)
    env = child_env(settings)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = await asyncio.create_subprocess_exec(
        settings.python,
        "-m",
        module,
        "--workspace",
        str(settings.workspace),
        "--json",
        *args,
        cwd=str(settings.workspace),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    request.app.state.activity.touch()
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUTS[body.op])
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(504, f"{body.op} timed out") from None
    out, err = stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")
    lines = [line for line in (out or err).strip().splitlines() if line.strip()]
    try:
        payload = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        payload = {"ok": False, "error": (err or out)[-2000:]}
    if proc.returncode != 0 and payload.get("ok") is not False:
        payload = {"ok": False, "error": (err or out)[-2000:], **payload}
    payload.setdefault("ok", proc.returncode == 0)
    return payload
