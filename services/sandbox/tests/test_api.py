from __future__ import annotations

import io
import json
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from sandboxd.app import create_app
from sandboxd.bootstrap import bootstrap

from .conftest import AUTH


@pytest.fixture
def client(make_settings):
    settings = make_settings()
    bootstrap(settings)
    app = create_app(settings, run_bootstrap=False)
    with TestClient(app) as c:
        c.settings = settings
        yield c


def test_auth_required(client):
    assert client.get("/health").status_code == 200
    assert client.get("/status").status_code == 401
    assert client.get("/files/tree", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/files/tree", headers=AUTH).status_code == 200


def test_files_roundtrip(client):
    r = client.put("/files/content", headers=AUTH, json={"path": "notes/plan.md", "content": "# Plan\n"})
    assert r.status_code == 200, r.text
    mtime = r.json()["mtime"]
    assert client.get("/files/content", headers=AUTH, params={"path": "notes/plan.md"}).json()["content"] == "# Plan\n"
    # optimistic concurrency
    stale = client.put("/files/content", headers=AUTH, json={"path": "notes/plan.md", "content": "x", "expected_mtime": mtime - 10})
    assert stale.status_code == 409
    tree = client.get("/files/tree", headers=AUTH, params={"depth": 2}).json()
    names = {e["name"] for e in tree["entries"]}
    assert {"notes", "TOPIC.md", "papers", "custom"} <= names
    assert ".env" not in names  # hidden by default
    hidden = client.get("/files/tree", headers=AUTH, params={"show_hidden": True}).json()
    env_entry = next(e for e in hidden["entries"] if e["name"] == ".env")
    assert env_entry["protected"] and env_entry["path"] == ".env"
    assert client.get("/files/content", headers=AUTH, params={"path": ".env"}).status_code == 403
    assert client.put("/files/content", headers=AUTH, json={"path": ".env", "content": "X=1"}).status_code == 403
    assert client.get("/files/content", headers=AUTH, params={"path": "../../etc/passwd"}).status_code == 400

    up = client.post(
        "/files/upload", headers=AUTH, data={"directory": "user_data"},
        files=[("files", ("survey.csv", b"id,score\n1,5\n", "text/csv"))],
    )
    assert up.status_code == 200 and up.json()["saved"][0]["path"] == "user_data/survey.csv"
    assert client.get("/files/raw", headers=AUTH, params={"path": "user_data/survey.csv"}).content.startswith(b"id,score")
    assert client.post("/files/move", headers=AUTH, json={"src": "user_data/survey.csv", "dst": "user_data/s.csv"}).status_code == 200
    assert client.delete("/files", headers=AUTH, params={"path": "notes"}).status_code == 409
    assert client.delete("/files", headers=AUTH, params={"path": "notes", "recursive": True}).status_code == 200
    assert client.delete("/files", headers=AUTH, params={"path": ""}).status_code == 400

    archive = client.get("/files/archive", headers=AUTH)
    assert archive.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(archive.content)).namelist()
    assert "TOPIC.md" in names and "user_data/s.csv" in names
    assert ".env" not in names and not any(n.startswith(".git/") for n in names)


INIT_CONFIG = {
    "env_modules": [{"module_type": "SimpleSocialSpace", "kwargs": {"agent_id_name_pairs": [[1, "Alice"], [2, "Bob"]]}}],
    "agents": [
        {"agent_id": 1, "agent_type": "PersonAgent", "kwargs": {"id": 1, "name": "Alice"}},
        {"agent_id": 2, "agent_type": "PersonAgent", "kwargs": {"id": 2, "name": "Bob"}},
    ],
}
STEPS = {"start_t": "2026-01-01T08:00:00", "steps": [{"type": "run", "num_steps": 1, "tick": 3600}, {"type": "ask", "question": "How do you feel?"}]}


def test_experiment_config_and_failed_run_lifecycle(client):
    bad = client.put("/experiments/1/1/config", headers=AUTH, json={"init_config": {"agents": []}, "steps": {"steps": []}})
    assert bad.status_code == 200 and bad.json()["valid"] is False
    assert {e["file"] for e in bad.json()["errors"]} == {"init_config.json", "steps.yaml"}

    ok = client.put("/experiments/1/1/config", headers=AUTH, json={"init_config": INIT_CONFIG, "steps": STEPS})
    assert ok.status_code == 200 and ok.json()["valid"], ok.text
    ws = client.settings.workspace
    assert (ws / "hypothesis_1/experiment_1/init/init_config.json").exists()

    listing = client.get("/experiments", headers=AUTH).json()["hypotheses"]
    assert listing[0]["experiments"][0]["status"] == "configured"
    assert client.post("/experiments/1/1/stop", headers=AUTH).json()["stopped"] is False
    assert client.get("/experiments/1/x y", headers=AUTH).status_code == 400

    # The LLM endpoint is unreachable in this test, so the run must end as failed.
    started = client.post("/experiments/1/1/run", headers=AUTH, json={})
    assert started.status_code == 200, started.text
    assert client.post("/experiments/1/1/run", headers=AUTH, json={}).status_code in (200, 409)
    deadline = time.time() + 180
    status = None
    while time.time() < deadline:
        status = client.get("/experiments/1/1", headers=AUTH).json()["status"]
        if status in {"failed", "completed"}:
            break
        time.sleep(1)
    assert status == "failed"
    logs = client.get("/experiments/1/1/logs", headers=AUTH, params={"source": "output.log", "tail": 50}).json()
    assert logs["exists"]
    with client.stream("GET", "/experiments/1/1/logs/stream", headers=AUTH, params={"tail": 5}) as stream:
        body = "".join(stream.iter_text())
    assert "event: end" in body
    assert client.delete("/experiments/1/1/run", headers=AUTH).json()["reset"] is True


def test_tools_runner(client):
    r = client.post("/tools/run", headers=AUTH, json={"tool": "research-pipeline", "args": ["where-am-i", "--json"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["json"]["current_stage"] == "literature_search"
    assert client.post("/tools/run", headers=AUTH, json={"tool": "rm", "args": ["-rf", "/"]}).status_code == 400
    jobs = client.get("/tools/jobs", headers=AUTH).json()["jobs"]
    assert jobs and jobs[0]["id"] == body["id"]


def test_workspace_settings_and_info(client):
    info = client.get("/workspace/info", headers=AUTH).json()
    assert info["initialized"] and info["progress"]["workspace"]["topic"] == "Echo chambers"
    assert client.put("/workspace/settings", headers=AUTH, json={"overrides": {"AGENTSOCIETY_LLM_API_BASE": "http://evil"}}).status_code == 400
    r = client.put("/workspace/settings", headers=AUTH, json={"overrides": {"AGENTSOCIETY_LLM_MODEL": "deepseek-v4-flash"}})
    assert r.status_code == 200
    settings = client.get("/workspace/settings", headers=AUTH).json()
    assert settings["effective"]["AGENTSOCIETY_LLM_MODEL"] == "deepseek-v4-flash"
    assert settings["effective"]["AGENTSOCIETY_LLM_API_KEY"].endswith("…")
    env_text = (client.settings.workspace / ".env").read_text()
    assert "AGENTSOCIETY_LLM_MODEL=deepseek-v4-flash" in env_text
    assert client.put("/workspace/user-env", headers=AUTH, json={"values": {"MY_KEY": 'va"l'}}).status_code == 200
    assert client.put("/workspace/user-env", headers=AUTH, json={"values": {"AGENTSOCIETY_LLM_API_KEY": "x"}}).status_code == 400
    env_text = (client.settings.workspace / ".env").read_text()
    assert 'MY_KEY="va\\"l"' in env_text and env_text.count("AGENTSOCIETY_LLM_API_KEY=") == 1


def test_workspace_import_is_safe(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("shared/TOPIC_NOTE.md", "imported")
        zf.writestr("shared/.env", "SECRET=1")
        zf.writestr("shared/../../escape.txt", "nope")
    buf.seek(0)
    r = client.post("/workspace/import", headers=AUTH, files={"archive": ("share.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 200, r.text
    ws = client.settings.workspace
    assert (ws / "TOPIC_NOTE.md").read_text() == "imported"
    assert "SECRET=1" not in (ws / ".env").read_text()
    assert not (ws.parent.parent / "escape.txt").exists()


def _wait_idle(client, sid, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        meta = client.get(f"/chat/sessions/{sid}", headers=AUTH).json()
        if meta["status"] == "idle" and meta["turns"] > 0:
            return meta
        time.sleep(0.2)
    raise AssertionError("chat turn did not finish")


def test_chat_turns_resume_and_stream(client):
    sid = client.post("/chat/sessions", headers=AUTH, json={}).json()["id"]
    r = client.post(f"/chat/sessions/{sid}/messages", headers=AUTH, json={"content": "Find papers on echo chambers"})
    assert r.status_code == 200, r.text
    meta = _wait_idle(client, sid)
    assert meta["claude_session_id"] == "sess-fake-1"
    assert meta["title"].startswith("Find papers")
    events = client.get(f"/chat/sessions/{sid}/events", headers=AUTH).json()["events"]
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "user" and kinds[-1] == "turn_end" and "claude" in kinds
    result = next(e["data"] for e in events if e["kind"] == "claude" and e["data"]["type"] == "result")
    assert result["seen_env"]["ANTHROPIC_BASE_URL"].endswith("/llm/anthropic")
    assert result["seen_env"]["ANTHROPIC_AUTH_TOKEN"] == "sbx_project_token"
    assert result["seen_env"]["SANDBOX_AGENT_SECRET"] is None
    assert result["permission_mode"] == "bypassPermissions" and result["resumed"] is False

    # second turn resumes the Claude session
    client.post(f"/chat/sessions/{sid}/messages", headers=AUTH, json={"content": "continue"})
    meta = _wait_idle(client, sid)
    events = client.get(f"/chat/sessions/{sid}/events", headers=AUTH, params={"after": len(events)}).json()["events"]
    result = next(e["data"] for e in events if e["kind"] == "claude" and e["data"]["type"] == "result")
    assert result["resumed"] is True

    with client.stream("GET", f"/chat/sessions/{sid}/stream", headers=AUTH, params={"after": 0}) as stream:
        body = "".join(stream.iter_text())
    assert body.count("event: turn_end") == 2 and "event: idle" in body

    # a failing CLI run is reported, not swallowed
    sid2 = client.post("/chat/sessions", headers=AUTH, json={"title": "t"}).json()["id"]
    client.post(f"/chat/sessions/{sid2}/messages", headers=AUTH, json={"content": "fail"})
    meta = _wait_idle(client, sid2)
    assert "boom from fake claude" in (meta["last_error"] or "")
    assert client.post(f"/chat/sessions/{sid}/messages", headers=AUTH, json={"content": "x", "permission_mode": "yolo"}).status_code == 400
    assert client.delete(f"/chat/sessions/{sid2}", headers=AUTH).status_code == 200
    listed = [s["id"] for s in client.get("/chat/sessions", headers=AUTH).json()["sessions"]]
    assert sid in listed and sid2 not in listed


def test_status_reports_busy(client):
    status = client.get("/status", headers=AUTH).json()
    assert status["busy"] is False and status["backend"] is None


def test_upstream_backend_through_proxy(make_settings):
    settings = make_settings(enable_backend=True, backend_port=18765)
    bootstrap(settings)
    app = create_app(settings, run_bootstrap=False)
    with TestClient(app) as c:
        deadline = time.time() + 90
        while time.time() < deadline:
            r = c.get("/as2/health", headers=AUTH)
            if r.status_code == 200:
                break
            time.sleep(1)
        assert r.status_code == 200, r.text
        modules = c.get("/as2/api/v1/modules/all", headers=AUTH).json()
        assert modules["success"] and "PersonAgent" in modules["agents"]
        # upstream rejects foreign workspaces; the proxy always pins ours
        prefill = c.get("/as2/api/v1/prefill-params", headers=AUTH, params={"workspace_path": "/etc"})
        assert prefill.status_code == 200, prefill.text
        custom = c.post("/as2/api/v1/custom/scan", headers=AUTH, json={"workspace_path": "/etc"})
        assert custom.status_code == 200 and json.loads(custom.text)["success"], custom.text
        status = c.get("/status", headers=AUTH).json()
        assert status["backend"]["running"]
