"""Run upstream workspace tools (``.agentsociety/bin/ags.py <tool> ...``) as jobs.

Every capability of the upstream CLI skills (literature search, hypotheses,
experiment-config validation, research-pipeline state, analysis harness, module
scanning, datasets) is reachable from the web app through this one endpoint
instead of being re-implemented. Arguments are passed as an argv list - never
through a shell - and the tool name must be one of the launcher's own tools.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..security import AgentAuth
from ..supervisor import terminate_group
from ..workspace_env import child_env

router = APIRouter(prefix="/tools", tags=["tools"], dependencies=[AgentAuth])

# Must match TOOL_SCRIPTS in upstream extension/runtime/agentsociety/bin/ags.py
# (tests/test_tools.py asserts the two stay in sync).
ALLOWED_TOOLS = frozenset(
    {
        "literature-search",
        "literature-full-text",
        "scan-modules",
        "hypothesis",
        "experiment-config",
        "run-experiment",
        "research-pipeline",
        "analysis",
        "create-agent",
        "create-env-module-validate",
        "create-env-module-resolve-sources",
        "use-dataset",
        "create-dataset",
    }
)
MAX_ARGS = 64
MAX_ARG_LEN = 64 * 1024


@dataclass
class Job:
    id: str
    tool: str
    args: list[str]
    log_path: Path
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    exit_code: int | None = None
    status: str = "running"
    process: asyncio.subprocess.Process | None = None
    task: asyncio.Task | None = None

    def summary(self, *, include_output: bool = False, max_bytes: int = 256 * 1024) -> dict:
        data = {
            "id": self.id,
            "tool": self.tool,
            "args": self.args,
            "status": self.status,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        if include_output:
            data["output"] = self.read_output(max_bytes)
            parsed = _maybe_json(data["output"])
            if parsed is not None:
                data["json"] = parsed
        return data

    def read_output(self, max_bytes: int) -> str:
        if not self.log_path.exists():
            return ""
        size = self.log_path.stat().st_size
        with open(self.log_path, "rb") as fh:
            fh.seek(max(0, size - max_bytes))
            return fh.read().decode("utf-8", errors="replace")


def _maybe_json(text: str):
    text = text.strip()
    if not text or text[0] not in "{[":
        return None
    with contextlib.suppress(json.JSONDecodeError):
        return json.loads(text)
    return None


class JobManager:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.jobs: dict[str, Job] = {}
        self.dir = settings.state_dir / "jobs"

    def active(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.status == "running"]

    async def start(self, tool: str, args: list[str], timeout: float) -> Job:
        launcher = self.settings.workspace / ".agentsociety" / "bin" / "ags.py"
        if not launcher.exists():
            raise HTTPException(409, "Workspace launcher missing; initialise the workspace first")
        self.dir.mkdir(parents=True, exist_ok=True)
        job = Job(id=uuid.uuid4().hex[:12], tool=tool, args=args, log_path=self.dir / f"{uuid.uuid4().hex}.log")
        log_file = open(job.log_path, "wb")  # noqa: SIM115 - handed to the child
        try:
            job.process = await asyncio.create_subprocess_exec(
                self.settings.python,
                str(launcher),
                tool,
                *args,
                cwd=str(self.settings.workspace),
                env=child_env(self.settings),
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_file.close()
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self._watch(job, timeout))
        self._prune()
        return job

    async def _watch(self, job: Job, timeout: float) -> None:
        assert job.process is not None
        try:
            job.exit_code = await asyncio.wait_for(job.process.wait(), timeout=timeout)
            if job.status == "running":  # cancel() may have marked it already
                job.status = "succeeded" if job.exit_code == 0 else "failed"
        except TimeoutError:
            await asyncio.to_thread(terminate_group, job.process.pid, grace=5.0)
            job.exit_code = await job.process.wait()
            job.status = "timeout"
        except asyncio.CancelledError:
            await asyncio.to_thread(terminate_group, job.process.pid, grace=5.0)
            job.status = "cancelled"
            raise
        finally:
            job.finished_at = time.time()

    async def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status == "running" and job.process is not None:
            await asyncio.to_thread(terminate_group, job.process.pid, grace=5.0)
            job.status = "cancelled"
        return job

    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        return job

    def _prune(self, keep: int = 200) -> None:
        finished = sorted((j for j in self.jobs.values() if j.status != "running"), key=lambda j: j.started_at)
        for job in finished[: max(0, len(self.jobs) - keep)]:
            self.jobs.pop(job.id, None)
            job.log_path.unlink(missing_ok=True)


class RunToolBody(BaseModel):
    tool: str
    args: list[str] = Field(default_factory=list)
    wait: bool = True
    timeout: float = Field(600, gt=0, le=6 * 3600)


@router.post("/run")
async def run_tool(request: Request, body: RunToolBody):
    tool = body.tool.strip().lower().replace("_", "-")
    if tool not in ALLOWED_TOOLS:
        raise HTTPException(400, f"Unknown tool {body.tool!r}; allowed: {sorted(ALLOWED_TOOLS)}")
    if len(body.args) > MAX_ARGS or any(len(a) > MAX_ARG_LEN or "\0" in a for a in body.args):
        raise HTTPException(400, "Arguments too long or invalid")
    manager: JobManager = request.app.state.jobs
    job = await manager.start(tool, body.args, body.timeout)
    request.app.state.activity.touch()
    if body.wait:
        wait_for = min(body.timeout, 120.0)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(job.task), timeout=wait_for)
    return job.summary(include_output=job.status != "running")


@router.get("/jobs")
async def list_jobs(request: Request):
    manager: JobManager = request.app.state.jobs
    return {"jobs": [j.summary() for j in sorted(manager.jobs.values(), key=lambda j: -j.started_at)]}


@router.get("/jobs/{job_id}")
async def get_job(request: Request, job_id: str):
    return request.app.state.jobs.get(job_id).summary(include_output=True)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str):
    job = await request.app.state.jobs.cancel(job_id)
    return job.summary()
