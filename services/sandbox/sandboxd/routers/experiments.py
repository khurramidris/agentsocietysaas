"""Experiment lifecycle: discover, configure, validate, run, stop, stream logs.

Layout and status semantics mirror upstream exactly
(``agentsociety2/skills/experiment/{config,runner}.py`` and the
``agentsociety-run-experiment`` skill): ``hypothesis_{h}/experiment_{e}/init``
holds ``init_config.json`` + ``steps.yaml`` and ``run/`` receives ``pid.json``,
``output.log``, ``stdout.log``/``stderr.log``, ``replay/`` and ``artifacts/``.
A run started here is indistinguishable from one the AI agent or the IDE starts.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..security import AgentAuth
from ..supervisor import pid_alive, terminate_group
from ..workspace_env import child_env

router = APIRouter(prefix="/experiments", tags=["experiments"], dependencies=[AgentAuth])

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
LOG_SOURCES = ("output.log", "stdout.log", "stderr.log")

# Children we spawned in this agent process, so we can reap them.
_children: dict[tuple[str, str], subprocess.Popen] = {}


def _check_id(value: str, field: str) -> str:
    if not ID_RE.fullmatch(value):
        raise HTTPException(400, f"Invalid {field}: {value!r}")
    return value


def experiment_paths(workspace: Path, h: str, e: str) -> dict[str, Path]:
    _check_id(h, "hypothesis_id")
    _check_id(e, "experiment_id")
    hyp = workspace / f"hypothesis_{h}"
    exp = hyp / f"experiment_{e}"
    run = exp / "run"
    return {
        "hypothesis": hyp,
        "experiment": exp,
        "init": exp / "init",
        "init_config": exp / "init" / "init_config.json",
        "steps": exp / "init" / "steps.yaml",
        "config_params": exp / "init" / "config_params.py",
        "experiment_md": exp / "EXPERIMENT.md",
        "hypothesis_md": hyp / "HYPOTHESIS.md",
        "sim_settings": hyp / "SIM_SETTINGS.json",
        "run": run,
        "pid_file": run / "pid.json",
        "replay_schema": run / "replay" / "_schema.json",
        "artifacts": run / "artifacts",
        "llm_stats": run / "LLM_STATS.json",
    }


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, json.JSONDecodeError):
        return None


def _reap() -> None:
    for key, proc in list(_children.items()):
        if proc.poll() is not None:
            _children.pop(key, None)


def experiment_status(workspace: Path, h: str, e: str) -> dict:
    """Port of upstream ``get_experiment_status`` plus progress fields from pid.json."""
    _reap()
    p = experiment_paths(workspace, h, e)
    status = "not_initialized"
    if p["init_config"].exists():
        status = "configured"
    if p["replay_schema"].exists():
        status = "completed"
    pid_data = _read_json(p["pid_file"]) or {}
    pid = pid_data.get("pid")
    pid_status = str(pid_data.get("status") or "").strip().lower()
    is_running = False
    if pid_status == "completed":
        status = "completed"
    elif pid_status == "failed":
        status = "failed"
    elif pid_status:
        status = pid_status
    if pid and pid_status not in {"completed", "failed", "stopped"}:
        if pid_alive(int(pid)):
            is_running = True
            status = "running"
        elif pid_status == "running":
            status = "completed" if p["replay_schema"].exists() else "failed"
    return {
        "hypothesis_id": h,
        "experiment_id": e,
        "status": status,
        "is_running": is_running,
        "pid": pid if is_running else None,
        "start_time": pid_data.get("start_time"),
        "end_time": pid_data.get("end_time"),
        "simulation_time": pid_data.get("simulation_time"),
        "step_count": pid_data.get("step_count"),
        "error": pid_data.get("error"),
        "resumed": pid_data.get("resumed", False),
        "has_init_config": p["init_config"].exists(),
        "has_steps": p["steps"].exists(),
        "has_replay": p["replay_schema"].exists(),
    }


def running_experiments(workspace: Path) -> list[dict]:
    out = []
    for hyp_dir in sorted(workspace.glob("hypothesis_*")):
        for exp_dir in sorted(hyp_dir.glob("experiment_*")):
            h = hyp_dir.name.removeprefix("hypothesis_")
            e = exp_dir.name.removeprefix("experiment_")
            if ID_RE.fullmatch(h) and ID_RE.fullmatch(e):
                st = experiment_status(workspace, h, e)
                if st["is_running"]:
                    out.append(st)
    return out


def _settings(request: Request):
    return request.app.state.settings


@router.get("")
async def list_experiments(request: Request):
    ws = _settings(request).workspace
    hypotheses = []
    for hyp_dir in sorted(ws.glob("hypothesis_*"), key=lambda p: p.name):
        if not hyp_dir.is_dir():
            continue
        h = hyp_dir.name.removeprefix("hypothesis_")
        if not ID_RE.fullmatch(h):
            continue
        title = None
        md = hyp_dir / "HYPOTHESIS.md"
        if md.exists():
            for line in md.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip().startswith("#"):
                    title = line.strip().lstrip("#").strip()
                    break
        experiments = []
        for exp_dir in sorted(hyp_dir.glob("experiment_*"), key=lambda p: p.name):
            e = exp_dir.name.removeprefix("experiment_")
            if exp_dir.is_dir() and ID_RE.fullmatch(e):
                experiments.append(experiment_status(ws, h, e))
        hypotheses.append({"hypothesis_id": h, "title": title, "has_hypothesis_md": md.exists(), "experiments": experiments})
    return {"hypotheses": hypotheses}


@router.get("/{h}/{e}")
async def get_experiment(request: Request, h: str, e: str):
    ws = _settings(request).workspace
    p = experiment_paths(ws, h, e)
    if not p["experiment"].exists():
        raise HTTPException(404, "Experiment not found")
    steps = None
    if p["steps"].exists():
        try:
            steps = yaml.safe_load(p["steps"].read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            steps = {"_error": f"invalid YAML: {exc}"}
    artifacts = []
    if p["artifacts"].is_dir():
        artifacts = [{"name": a.name, "size": a.stat().st_size, "mtime": a.stat().st_mtime} for a in sorted(p["artifacts"].iterdir()) if a.is_file()]
    return {
        **experiment_status(ws, h, e),
        "init_config": _read_json(p["init_config"]),
        "steps": steps,
        "experiment_md": p["experiment_md"].read_text(encoding="utf-8") if p["experiment_md"].exists() else None,
        "llm_stats": _read_json(p["llm_stats"]),
        "artifacts": artifacts,
    }


_VALIDATE_SCRIPT = """
import json, sys, yaml
from agentsociety2.society.models import InitConfig, StepsConfig
payload = json.loads(sys.stdin.read())
errors = []
try:
    InitConfig.model_validate(payload["init_config"])
except Exception as exc:
    errors.append({"file": "init_config.json", "error": str(exc)})
try:
    StepsConfig.model_validate(payload["steps"])
except Exception as exc:
    errors.append({"file": "steps.yaml", "error": str(exc)})
print(json.dumps({"valid": not errors, "errors": errors}))
"""


def _validate_models(settings, init_config: dict, steps: dict) -> dict:
    env = child_env(settings)
    env.setdefault("AGENTSOCIETY_LLM_API_KEY", "not-configured")
    env.setdefault("AGENTSOCIETY_LLM_API_BASE", "http://127.0.0.1:9/v1")
    proc = subprocess.run(
        [settings.python, "-c", _VALIDATE_SCRIPT],
        input=json.dumps({"init_config": init_config, "steps": steps}),
        capture_output=True,
        text=True,
        env=env,
        cwd=settings.workspace,
        timeout=120,
    )
    if proc.returncode != 0:
        raise HTTPException(500, f"Validator failed: {proc.stderr.strip()[-1500:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


class ConfigBody(BaseModel):
    init_config: dict
    steps: dict | str = Field(..., description="steps.yaml content as a mapping or YAML text")
    experiment_md: str | None = None
    validate_only: bool = False


@router.put("/{h}/{e}/config")
async def put_config(request: Request, h: str, e: str, body: ConfigBody):
    """Write ``init/init_config.json`` and ``init/steps.yaml`` after schema validation."""
    settings = _settings(request)
    p = experiment_paths(settings.workspace, h, e)
    steps = body.steps
    if isinstance(steps, str):
        try:
            steps = yaml.safe_load(steps) or {}
        except yaml.YAMLError as exc:
            raise HTTPException(422, f"steps is not valid YAML: {exc}") from None
    result = await asyncio.to_thread(_validate_models, settings, body.init_config, steps)
    if not result["valid"] or body.validate_only:
        return result
    if experiment_status(settings.workspace, h, e)["is_running"]:
        raise HTTPException(409, "Experiment is running")
    p["init"].mkdir(parents=True, exist_ok=True)
    p["init_config"].write_text(json.dumps(body.init_config, indent=2, ensure_ascii=False), encoding="utf-8")
    p["steps"].write_text(yaml.safe_dump(steps, allow_unicode=True, sort_keys=False), encoding="utf-8")
    if body.experiment_md is not None:
        p["experiment_md"].write_text(body.experiment_md, encoding="utf-8")
    return {**result, "written": [str(p["init_config"].relative_to(settings.workspace)), str(p["steps"].relative_to(settings.workspace))]}


class RunBody(BaseModel):
    resume: bool = False
    batch_size: int | None = Field(None, ge=1, le=100000)
    replay_disable: bool = False
    log_level: str = Field("INFO", pattern="^(DEBUG|INFO|WARNING|ERROR)$")


def _write_pid(run_dir: Path, pid: int, experiment_key: str) -> None:
    (run_dir / "pid.json").write_text(
        json.dumps(
            {"pid": pid, "status": "running", "start_time": datetime.now(UTC).isoformat(), "experiment_id": experiment_key},
            indent=2,
        ),
        encoding="utf-8",
    )


@router.post("/{h}/{e}/run")
async def start_run(request: Request, h: str, e: str, body: RunBody | None = None):
    body = body or RunBody()
    settings = _settings(request)
    ws = settings.workspace
    p = experiment_paths(ws, h, e)
    if not p["init_config"].exists() or not p["steps"].exists():
        raise HTTPException(409, "init/init_config.json and init/steps.yaml are required")
    if experiment_status(ws, h, e)["is_running"]:
        raise HTTPException(409, "Experiment is already running")
    run_dir = p["run"]
    run_dir.mkdir(parents=True, exist_ok=True)
    experiment_key = f"{h}_{e}"
    argv = [
        settings.python,
        "-m",
        "agentsociety2.society.cli",
        "--config",
        str(p["init_config"]),
        "--steps",
        str(p["steps"]),
        "--run-dir",
        str(run_dir),
        "--experiment-id",
        experiment_key,
        "--log-level",
        body.log_level,
        "--log-file",
        str(run_dir / "output.log"),
    ]
    if body.resume:
        argv.append("--resume")
    if body.batch_size:
        argv += ["--batch-size", str(body.batch_size)]
    if body.replay_disable:
        argv.append("--replay-disable")
    with open(run_dir / "stdout.log", "w", encoding="utf-8") as out, open(run_dir / "stderr.log", "w", encoding="utf-8") as err:
        proc = subprocess.Popen(  # noqa: ASYNC220 - spawning returns immediately
            argv,
            cwd=ws,
            env=child_env(settings),
            stdout=out,
            stderr=err,
            start_new_session=True,
            close_fds=True,
        )
    _children[(h, e)] = proc
    _write_pid(run_dir, proc.pid, experiment_key)
    request.app.state.activity.touch()
    return {"started": True, "pid": proc.pid, "experiment_id": experiment_key, "argv": argv[1:]}


@router.post("/{h}/{e}/stop")
async def stop_run(request: Request, h: str, e: str):
    ws = _settings(request).workspace
    p = experiment_paths(ws, h, e)
    st = experiment_status(ws, h, e)
    if not st["is_running"]:
        return {"stopped": False, "status": st["status"]}
    await asyncio.to_thread(terminate_group, int(st["pid"]), grace=15.0)
    _reap()
    data = _read_json(p["pid_file"]) or {}
    if str(data.get("status", "")).lower() not in {"completed", "failed"}:
        data.update({"status": "stopped", "end_time": datetime.now(UTC).isoformat()})
        p["pid_file"].write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {"stopped": True, "status": experiment_status(ws, h, e)["status"]}


@router.delete("/{h}/{e}/run")
async def reset_run(request: Request, h: str, e: str):
    """Delete ``run/`` so the experiment can be re-run from scratch."""
    ws = _settings(request).workspace
    p = experiment_paths(ws, h, e)
    if experiment_status(ws, h, e)["is_running"]:
        raise HTTPException(409, "Experiment is running")
    if p["run"].exists():
        shutil.rmtree(p["run"])
    return {"reset": True}


def tail_lines(path: Path, lines: int) -> str:
    """Last ``lines`` lines of a file, read backwards in chunks (multi-MB safe)."""
    if not path.exists():
        return ""
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        end = fh.tell()
        chunk = 64 * 1024
        buf = b""
        pos = end
        while pos > 0 and buf.count(b"\n") <= lines:
            step = min(chunk, pos)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + buf
    text = buf.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


def _log_path(p: dict[str, Path], source: str) -> Path:
    if source == "auto":
        for name in LOG_SOURCES:
            if (p["run"] / name).exists():
                return p["run"] / name
        return p["run"] / "output.log"
    if source not in LOG_SOURCES:
        raise HTTPException(400, f"source must be one of {('auto', *LOG_SOURCES)}")
    return p["run"] / source


@router.get("/{h}/{e}/logs")
async def get_logs(request: Request, h: str, e: str, source: str = Query("auto"), tail: int = Query(500, ge=1, le=100000)):
    p = experiment_paths(_settings(request).workspace, h, e)
    path = _log_path(p, source)
    return {"source": path.name, "exists": path.exists(), "content": tail_lines(path, tail)}


@router.get("/{h}/{e}/logs/stream")
async def stream_logs(request: Request, h: str, e: str, source: str = Query("auto"), tail: int = Query(200, ge=0, le=10000)):
    """Server-sent events: the last ``tail`` lines, then new lines until the run ends."""
    ws = _settings(request).workspace
    p = experiment_paths(ws, h, e)
    path = _log_path(p, source)

    async def events():
        initial = tail_lines(path, tail) if tail else ""
        for line in initial.splitlines():
            yield f"data: {json.dumps({'line': line})}\n\n"
        offset = path.stat().st_size if path.exists() else 0
        idle_after_exit = 0.0
        pending = b""
        last_send = asyncio.get_running_loop().time()
        while True:
            if await request.is_disconnected():
                return
            if path.exists():
                size = path.stat().st_size
                if size < offset:  # truncated / restarted
                    offset = 0
                if size > offset:
                    with open(path, "rb") as fh:
                        fh.seek(offset)
                        data = fh.read(size - offset)
                    offset = size
                    pending += data
                    *complete, pending = pending.split(b"\n")
                    for raw in complete:
                        yield f"data: {json.dumps({'line': raw.decode('utf-8', errors='replace')})}\n\n"
                    last_send = asyncio.get_running_loop().time()
            running = experiment_status(ws, h, e)["is_running"]
            if not running:
                idle_after_exit += 0.5
                if idle_after_exit >= 2.0:
                    if pending:
                        yield f"data: {json.dumps({'line': pending.decode('utf-8', errors='replace')})}\n\n"
                    st = experiment_status(ws, h, e)
                    yield f"event: end\ndata: {json.dumps(st)}\n\n"
                    return
            elif asyncio.get_running_loop().time() - last_send > 15:
                yield ": keep-alive\n\n"
                last_send = asyncio.get_running_loop().time()
            await asyncio.sleep(0.5)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
