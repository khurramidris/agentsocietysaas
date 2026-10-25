"""Dataset hub: author flow, validation, visibility, review - and the upstream skill CLIs."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx
import pytest

from controlplane.api import datasets as datasets_api
from controlplane.models import User

from .conftest import REPO, live_api_key, live_signup

SKILLS = REPO / ".data" / "assets" / "extension" / "skills"
CREATE = SKILLS / "agentsociety-create-dataset" / "v1.0.0" / "scripts" / "create.py"
USE = SKILLS / "agentsociety-use-dataset" / "v1.0.0" / "scripts" / "use.py"


def package(dataset_id: str = "survey-2026", *, meta: dict | None = None, extra: dict[str, bytes] | None = None, top: str | None = None) -> bytes:
    import json

    top = dataset_id if top is None else top
    meta = meta or {
        "id": dataset_id,
        "name": "Opinion survey 2026",
        "description": "Panel answers",
        "category": "surveys",
        "version": "1.2.0",
        "tags": ["survey", "polarization"],
        "author": "Lab",
        "license": "CC BY 4.0",
    }
    files = {
        "dataset.json": json.dumps(meta).encode(),
        "README.md": b"# Opinion survey\n\nAnswers of 1,000 panelists.",
        "data/answers.csv": b"id,answer\n1,yes\n2,no\n",
        **(extra or {}),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(f"{top}/{name}" if top else name, data)
    return buf.getvalue()


async def upload(cp, h: dict, blob: bytes, dataset_id: str = "survey-2026") -> httpx.Response:
    r = await cp.client.post("/api/user/data/datasets", json={"id": dataset_id, "name": dataset_id, "category": "surveys"}, headers=h)
    assert r.status_code == 200, r.text
    init = (await cp.client.post(f"/api/user/data/datasets/{dataset_id}/init-upload", json={"file_size": len(blob)}, headers=h)).json()
    for i, part in enumerate(init["part_urls"]):
        chunk = blob[i * init["part_size"] : (i + 1) * init["part_size"]]
        r = await cp.client.put(part["upload_url"], content=chunk, headers={"content-type": ""})
        assert r.status_code == 200, r.text
    return await cp.client.post(
        f"/api/user/data/datasets/{dataset_id}/complete-upload",
        json={"upload_id": init["upload_id"], "parts": [{"part_number": p["part_number"]} for p in init["part_urls"]]},
        headers=h,
    )


async def make_admin(cp, account) -> None:
    async with cp.ctx.db.sessionmaker() as db:
        (await db.get(User, account.user_id)).is_platform_admin = True
        await db.commit()


async def test_dataset_lifecycle_and_visibility(cp):
    alice = await cp.signup("alice@example.com")
    bob = await cp.signup("bob@example.com")
    blob = package()
    r = await upload(cp, alice.h, blob)
    assert r.status_code == 200, r.text
    ds = r.json()
    # Metadata comes from the package's dataset.json.
    assert (ds["status"], ds["version"], ds["tags"], ds["files"]) == ("uploaded", "1.2.0", ["survey", "polarization"], 3)

    # Private to the org until published.
    assert (await cp.client.get("/api/v1/data/datasets/survey-2026", headers=alice.h)).status_code == 200
    assert (await cp.client.get("/api/v1/data/datasets/survey-2026", headers=bob.h)).status_code == 404
    assert (await cp.client.get("/api/v1/data/datasets/survey-2026")).status_code == 404
    assert (await cp.client.get("/api/v1/data/datasets")).json()["total"] == 0
    assert (await cp.client.get("/api/v1/data/datasets", headers=alice.h)).json()["total"] == 1

    readme = (await cp.client.get("/api/v1/data/datasets/survey-2026/readme", headers=alice.h)).json()["content"]
    assert readme.startswith("# Opinion survey")
    files = (await cp.client.get("/api/v1/data/datasets/survey-2026/file-tree", headers=alice.h)).json()["files"]
    assert sorted(f["path"] for f in files) == ["README.md", "data/answers.csv", "dataset.json"]  # top folder stripped

    # Other orgs cannot touch it.
    assert (await cp.client.post("/api/user/data/datasets", json={"id": "survey-2026", "name": "mine"}, headers=bob.h)).status_code == 409
    assert (await cp.client.delete("/api/user/data/datasets/survey-2026", headers=bob.h)).status_code == 404

    # Review: submit -> admin approves -> public.
    assert (await cp.client.post("/api/user/data/datasets/survey-2026/submit", headers=alice.h)).json()["status"] == "pending_review"
    admin = await cp.signup("ops@example.com")
    await make_admin(cp, admin)
    queue = (await cp.client.get("/api/v1/admin/datasets", headers=admin.h)).json()["datasets"]
    assert [d["id"] for d in queue] == ["survey-2026"]
    assert (await cp.client.post("/api/v1/admin/datasets/survey-2026/review", json={"approve": True}, headers=alice.h)).status_code == 403
    r = await cp.client.post("/api/v1/admin/datasets/survey-2026/review", json={"approve": True, "note": "ok"}, headers=admin.h)
    assert r.json()["status"] == "published"
    listing = (await cp.client.get("/api/v1/data/datasets", params={"category": "surveys", "tags": "polarization"})).json()
    assert [d["id"] for d in listing["datasets"]] == ["survey-2026"]
    assert (await cp.client.get("/api/v1/data/datasets", params={"q": "nothing-matches"})).json()["total"] == 0

    # Anyone can download a published dataset through a signed URL.
    url = (await cp.client.post("/api/v1/data/datasets/survey-2026/download")).json()["url"]
    r = await cp.client.get(url)
    assert r.status_code == 200 and r.content == blob
    tampered = url.replace("sig=", "sig=0")
    assert (await cp.client.get(tampered)).status_code == 403

    # Re-uploading replaces the package; deleting removes it.
    r = await upload(cp, alice.h, package(meta={"id": "survey-2026", "name": "v2", "version": "2.0.0"}))
    assert r.json()["version"] == "2.0.0"
    assert (await cp.client.delete("/api/user/data/datasets/survey-2026", headers=alice.h)).status_code == 200
    assert (await cp.client.get("/api/v1/data/datasets/survey-2026")).status_code == 404


async def test_rejected_datasets_go_back_to_private(cp):
    alice = await cp.signup("alice@example.com")
    admin = await cp.signup("ops@example.com")
    await make_admin(cp, admin)
    await upload(cp, alice.h, package())
    await cp.client.post("/api/user/data/datasets/survey-2026/submit", headers=alice.h)
    r = await cp.client.post("/api/v1/admin/datasets/survey-2026/review", json={"approve": False, "note": "missing license"}, headers=admin.h)
    assert (r.json()["status"], r.json()["visibility"]) == ("rejected", "org")
    assert (await cp.client.get("/api/v1/data/datasets/survey-2026")).status_code == 404


@pytest.mark.parametrize(
    "blob, message",
    [
        (b"not a zip", "not a valid ZIP"),
        (package(meta={"id": "other-id", "name": "x"}), "does not match"),
        (package(extra={"../../evil.sh": b"rm -rf /"}), "unsafe paths"),
    ],
)
async def test_package_validation(cp, blob, message):
    alice = await cp.signup("alice@example.com")
    r = await upload(cp, alice.h, blob)
    assert r.status_code == 400 and message in r.json()["detail"]
    # A rejected package is discarded, not published.
    assert (await cp.client.get("/api/v1/data/datasets/survey-2026", headers=alice.h)).json()["package_size_bytes"] is None


async def test_package_without_metadata(cp):
    alice = await cp.signup("alice@example.com")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("data.csv", "a,b\n")
    r = await upload(cp, alice.h, buf.getvalue())
    assert r.status_code == 400 and "dataset.json not found" in r.json()["detail"]


async def test_upload_urls_are_signed_and_size_limited(cp, monkeypatch):
    alice = await cp.signup("alice@example.com")
    await cp.client.post("/api/user/data/datasets", json={"id": "big-one", "name": "big"}, headers=alice.h)
    init = (await cp.client.post("/api/user/data/datasets/big-one/init-upload", json={"file_size": 100}, headers=alice.h)).json()
    url = init["part_urls"][0]["upload_url"]
    assert (await cp.client.put(url.replace("part=1", "part=2"), content=b"x")).status_code == 403
    monkeypatch.setattr(datasets_api, "PART_SIZE", 10)
    assert (await cp.client.put(url, content=b"x" * 11)).status_code == 413
    r = await cp.client.post("/api/user/data/datasets/big-one/complete-upload", json={"upload_id": init["upload_id"]}, headers=alice.h)
    assert r.status_code == 400 and "missing part 1" in r.json()["detail"]
    r = await cp.client.post("/api/user/data/datasets/big-one/init-upload", json={"file_size": 6 * 1024**3}, headers=alice.h)
    assert r.status_code == 413


async def test_author_api_requires_identity(cp):
    r = await cp.client.post("/api/user/data/datasets", json={"id": "x1", "name": "x"})
    assert r.status_code == 401
    assert (await cp.client.get("/api/v1/data/datasets", headers={"authorization": "Bearer sbx_00_11"})).status_code == 401
    alice = await cp.signup("alice@example.com")
    r = await cp.client.post("/api/user/data/datasets", json={"id": "Bad Slug!", "name": "x"}, headers=alice.h)
    assert r.status_code == 400
    r = await cp.client.post("/api/user/data/datasets", json={"id": "okay-slug", "name": "x", "category": "weird"}, headers=alice.h)
    assert r.status_code == 400
    assert (await cp.client.get("/api/v1/data/categories")).json()["categories"][0] == {"value": "agent_profiles", "label": "Agent profiles"}


async def test_sandboxes_publish_into_their_org(cp):
    alice = await cp.signup("alice@example.com")
    project = await cp.create_project(alice)
    token = {"authorization": f"Bearer {cp.project_token(project['id'])}"}
    assert (await upload(cp, token, package())).status_code == 200
    # Visible to the org's members in the web app.
    mine = (await cp.client.get("/api/user/data/datasets", headers=alice.h)).json()["datasets"]
    assert [d["id"] for d in mine] == ["survey-2026"]


# --------------------------------------------------- the upstream skill CLIs


@pytest.mark.skipif(not CREATE.exists(), reason="sandbox assets not prepared (scripts/prepare_sandbox_assets.sh)")
def test_upstream_dataset_skills_against_the_hub(live_cp, tmp_path):
    alice = live_signup(live_cp, "alice@example.com")
    key = live_api_key(live_cp, alice)
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    base_env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "AGENTSOCIETY_DATASET_SERVER": live_cp.base_url}

    def run(script: Path, *args: str, token: str | None = key) -> str:
        env = {**base_env, **({"AGENTSOCIETY_DATASET_TOKEN": token} if token else {})}
        proc = subprocess.run([sys.executable, "-I", str(script), *args], cwd=work, env=env, capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, f"{script.name} {args}: {proc.stdout}\n{proc.stderr}"
        return proc.stdout

    run(CREATE, "init", "survey-2026", "--category", "surveys", "--description", "Opinion panel", "--author", "Lab", "--tags", "survey,polarization")
    (work / "survey-2026" / "data" / "answers.csv").write_text("id,answer\n1,yes\n2,no\n")
    assert "PASS" in run(CREATE, "validate", "survey-2026/")
    run(CREATE, "pack", "survey-2026")
    assert "Upload complete!" in run(CREATE, "upload", "survey-2026.zip")

    assert "survey-2026" in run(USE, "search")
    assert "Opinion panel" in run(USE, "info", "survey-2026")
    assert "survey-2026" in run(USE, "readme", "survey-2026")
    assert "answers.csv" in run(USE, "files", "survey-2026")
    (work / "datasets").mkdir()  # exists in every workspace (bootstrap); upstream expects it
    run(USE, "download", "survey-2026", "--output", "./datasets/")
    downloaded = list((work / "datasets").rglob("answers.csv"))
    assert downloaded and downloaded[0].read_text() == "id,answer\n1,yes\n2,no\n"

    # Without credentials the dataset is not visible until it is published.
    assert "survey-2026" not in run(USE, "search", token=None)
    assert "submitted for review" in run(CREATE, "submit", "survey-2026")

    async def approve():
        async with live_cp.app.state.ctx.db.sessionmaker() as db:
            (await db.get(User, alice.user_id)).is_platform_admin = True
            await db.commit()

    live_cp.run(approve())
    r = httpx.post(f"{live_cp.base_url}/api/v1/admin/datasets/survey-2026/review", json={"approve": True}, headers=alice.h)
    assert r.json()["status"] == "published"
    assert "survey-2026" in run(USE, "search", token=None)
