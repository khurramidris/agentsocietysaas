"""Sandbox drivers: Docker (fake engine), Kubernetes (manifests + fake API), local processes."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx
import pytest

from controlplane.sandboxes import local as local_driver
from controlplane.sandboxes.base import SandboxSpec
from controlplane.sandboxes.docker import DockerDriver, bridge_name, network_name
from controlplane.sandboxes.kubernetes import KubernetesDriver, pod_manifest, pvc_manifest
from controlplane.settings import Settings

from .conftest import REPO

PID = "prj_0123456789abcdef0123"


def spec(**kw) -> SandboxSpec:
    return SandboxSpec(
        project_id=PID, image="registry.example/sandbox:1.0", env={"SANDBOX_AGENT_SECRET": "s3cret", "PLATFORM_TOKEN": "sbx_a_b"}, cpus=2, memory_gb=4, **kw
    )


def settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


# --------------------------------------------------------------------- docker


class NotFound(Exception):
    """Named like docker.errors.NotFound, which the driver recognises."""


class FakeContainer:
    def __init__(self, engine: FakeEngine, kwargs: dict) -> None:
        self.engine = engine
        self.kwargs = kwargs
        self.name = kwargs["name"]
        self.id = "c0ffee" * 10
        self.status = "running"
        self.attrs = {"State": {"ExitCode": 0}, "NetworkSettings": {"Ports": {"7000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "49153"}]}}}

    def reload(self) -> None:
        pass

    def stop(self, timeout: int = 10) -> None:
        self.engine.log.append(("stop", self.name))
        self.status = "exited"

    def remove(self, force: bool = False) -> None:
        self.engine.log.append(("remove", self.name))
        self.engine.containers.items.pop(self.name, None)


class FakeNetwork:
    def __init__(self, engine: FakeEngine, name: str, **kw) -> None:
        self.engine, self.name, self.options = engine, name, kw
        self.attached: dict[str, list[str]] = {}

    def connect(self, container: str, aliases=None) -> None:
        if container in self.attached:
            raise RuntimeError(f"endpoint with name {container} already exists in network {self.name}")
        self.attached[container] = aliases or []

    def disconnect(self, container: str, force: bool = False) -> None:
        self.attached.pop(container, None)

    def remove(self) -> None:
        self.engine.log.append(("network-remove", self.name))
        self.engine.networks.items.pop(self.name, None)


class _Collection:
    def __init__(self) -> None:
        self.items: dict = {}

    def get(self, name):
        if name not in self.items:
            raise NotFound(name)
        return self.items[name]


class FakeEngine:
    def __init__(self) -> None:
        self.log: list = []
        engine = self

        class Containers(_Collection):
            def run(self, **kwargs):
                c = FakeContainer(engine, kwargs)
                self.items[c.name] = c
                engine.log.append(("run", c.name))
                return c

        class Networks(_Collection):
            def create(self, name, **kw):
                n = FakeNetwork(engine, name, **kw)
                self.items[name] = n
                return n

        class Volume:
            def __init__(self, name):
                self.name = name

            def remove(self, force=False):
                engine.log.append(("volume-remove", self.name))

        class Volumes(_Collection):
            pass

        self.containers = Containers()
        self.networks = Networks()
        self.volumes = Volumes()
        self.Volume = Volume


async def test_docker_network_mode_isolates_each_sandbox():
    engine = FakeEngine()
    driver = DockerDriver(settings(docker_access="network", docker_control_plane_container="cp-1"), client=engine)
    info = await driver.start(spec())
    name = spec().name
    assert info.state == "running" and info.endpoint == f"http://{name}:7000"

    net = engine.networks.items[network_name(PID)]
    assert net.options["labels"]["agentsociety.project"] == PID
    # The host firewall matches sandbox traffic by bridge name (deploy/docker/harden-host.sh).
    assert net.options["options"] == {"com.docker.network.bridge.name": bridge_name(PID)}
    assert bridge_name(PID).startswith("sbx-") and len(bridge_name(PID)) <= 15
    assert net.attached == {"cp-1": ["controlplane"]}  # the control plane joins under its alias
    kw = engine.containers.items[name].kwargs
    assert kw["network"] == network_name(PID) and "ports" not in kw and "extra_hosts" not in kw
    assert kw["cap_drop"] == ["ALL"] and kw["security_opt"] == ["no-new-privileges:true"]
    assert kw["pids_limit"] == 4096 and kw["mem_limit"] == kw["memswap_limit"] == "4096m" and kw["nano_cpus"] == 2_000_000_000
    assert kw["volumes"] == {spec().volume: {"bind": "/data", "mode": "rw"}}
    assert kw["environment"]["SANDBOX_WORKSPACE"] == "/data/workspace" and kw["environment"]["PLATFORM_TOKEN"] == "sbx_a_b"
    assert "runtime" not in kw

    # Restarting re-creates the container on the same network without re-attaching.
    await driver.start(spec())
    assert ("remove", name) in engine.log and net.attached == {"cp-1": ["controlplane"]}
    assert (await driver.inspect(PID)).state == "running"

    await driver.stop(PID)
    assert ("stop", name) in engine.log and network_name(PID) not in engine.networks.items
    assert (await driver.inspect(PID)).state == "stopped"
    engine.volumes.items[spec().volume] = engine.Volume(spec().volume)
    await driver.destroy(PID, delete_data=True)
    assert ("volume-remove", spec().volume) in engine.log


async def test_docker_network_mode_needs_a_containerised_control_plane(monkeypatch):
    monkeypatch.delenv("HOSTNAME", raising=False)
    driver = DockerDriver(settings(docker_access="network"), client=FakeEngine())
    with pytest.raises(RuntimeError, match="CP_DOCKER_CONTROL_PLANE_CONTAINER"):
        await driver.start(spec())


async def test_docker_port_mode_disables_inter_container_traffic():
    engine = FakeEngine()
    driver = DockerDriver(settings(docker_access="port", docker_runtime="runsc"), client=engine)
    info = await driver.start(spec())
    assert info.endpoint == "http://127.0.0.1:49153"
    shared = engine.networks.items["agentsociety-sandboxes"]
    assert shared.options["options"] == {"com.docker.network.bridge.enable_icc": "false", "com.docker.network.bridge.name": "sbx-shared"}
    kw = engine.containers.items[spec().name].kwargs
    assert kw["ports"] == {"7000/tcp": ("127.0.0.1", None)}  # never published beyond loopback
    assert kw["extra_hosts"] == {"host.docker.internal": "host-gateway"}
    assert kw["runtime"] == "runsc"


async def test_docker_inspect_reports_crashes():
    engine = FakeEngine()
    driver = DockerDriver(settings(docker_access="port"), client=engine)
    await driver.start(spec())
    container = engine.containers.items[spec().name]
    container.status = "exited"
    container.attrs["State"] = {"ExitCode": 137, "Error": "OOMKilled"}
    info = await driver.inspect(PID)
    assert info.state == "error" and "137" in info.detail and "OOMKilled" in info.detail


# ----------------------------------------------------------------- kubernetes


def test_pod_manifest_is_locked_down():
    s = settings(k8s_runtime_class="gvisor", k8s_storage_class="fast-ssd", k8s_volume_size="20Gi")
    pod = pod_manifest(s, spec())
    podspec = pod["spec"]
    container = podspec["containers"][0]
    assert podspec["automountServiceAccountToken"] is False and podspec["enableServiceLinks"] is False
    assert podspec["securityContext"]["runAsNonRoot"] is True and podspec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert podspec["runtimeClassName"] == "gvisor"
    assert container["securityContext"] == {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
    assert container["resources"]["limits"] == {"cpu": "2", "memory": "4096Mi"}
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["SANDBOX_WORKSPACE"] == "/data/workspace" and env["SANDBOX_AGENT_SECRET"] == "s3cret"
    assert [e["name"] for e in container["env"]] == sorted(env)
    mounts = {m["mountPath"] for m in container["volumeMounts"]}
    assert mounts == {"/data", "/tmp", "/dev/shm"}  # writable paths with a read-only root
    assert pod["metadata"]["labels"]["agentsociety/project"] == PID
    pvc = pvc_manifest(s, spec())
    assert pvc["spec"]["storageClassName"] == "fast-ssd" and pvc["spec"]["resources"]["requests"]["storage"] == "20Gi"
    assert "runtimeClassName" not in pod_manifest(settings(), spec())["spec"]


class ApiError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class FakeCoreApi:
    def __init__(self) -> None:
        self.pvcs: dict = {}
        self.pods: dict = {}
        self.conflicts = 2
        self.calls: list = []

    def read_namespaced_persistent_volume_claim(self, name, ns):
        if name not in self.pvcs:
            raise ApiError(404)
        return self.pvcs[name]

    def create_namespaced_persistent_volume_claim(self, ns, body):
        self.pvcs[body["metadata"]["name"]] = body

    def delete_namespaced_persistent_volume_claim(self, name, ns):
        self.pvcs.pop(name)

    def delete_namespaced_pod(self, name, ns, grace_period_seconds=None):
        self.calls.append(("delete", name))
        if name not in self.pods:
            raise ApiError(404)
        self.pods.pop(name)

    def create_namespaced_pod(self, ns, body):
        if self.conflicts:  # the previous pod is still terminating
            self.conflicts -= 1
            raise ApiError(409)
        self.pods[body["metadata"]["name"]] = body

    def read_namespaced_pod(self, name, ns):
        if name not in self.pods:
            raise ApiError(404)

        class Status:
            phase = "Running"
            pod_ip = "10.8.0.12"

        class Pod:
            status = Status()

        return Pod()


async def test_kubernetes_driver_lifecycle(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    api = FakeCoreApi()
    driver = KubernetesDriver(settings(), core_api=api)
    info = await driver.start(spec())
    assert info.state == "running" and info.endpoint == "http://10.8.0.12:7000"
    assert spec().volume in api.pvcs and spec().name in api.pods
    await driver.stop(PID)
    assert (await driver.inspect(PID)).state == "stopped"
    await driver.stop(PID)  # idempotent
    await driver.destroy(PID, delete_data=True)
    assert api.pvcs == {}


# ---------------------------------------------------------------------- local


def test_local_driver_does_not_leak_control_plane_secrets(monkeypatch):
    for key, value in {
        "CP_SECRET_KEY": "x",
        "ANTHROPIC_API_KEY": "x",
        "ANTHROPIC_BASE_URL": "x",
        "CLAUDE_CODE_SESSION": "x",
        "CCR_TOKEN": "x",
        "OPENAI_API_KEY": "x",
        "AWS_SECRET_ACCESS_KEY": "x",
        "GITHUB_TOKEN": "x",
        "DB_PASSWORD": "x",
        "MY_PRIVATE_THING": "x",
        "LANG": "C.UTF-8",
    }.items():
        monkeypatch.setenv(key, value)
    env = local_driver._inheritable_env()
    assert "PATH" in env and env["LANG"] == "C.UTF-8"
    leaked = [
        k
        for k in env
        if k.startswith(("CP_", "ANTHROPIC_", "CLAUDE", "CCR_", "OPENAI_")) or any(m in k for m in ("SECRET", "TOKEN", "PASSWORD", "PRIVATE", "API_KEY"))
    ]
    assert leaked == []


ASSETS = REPO / ".data" / "assets" / "extension"


@pytest.mark.skipif(not (ASSETS / "skills").exists(), reason="sandbox assets not prepared (scripts/prepare_sandbox_assets.sh)")
async def test_local_driver_runs_sandboxd_and_survives_control_plane_restarts(tmp_path):
    s = settings(data_dir=tmp_path, local_sandbox_python=sys.executable, local_sandbox_assets_dir=ASSETS, local_sandbox_port_range=(17500, 17600))
    # The manager's build_env supplies these; the driver adds ports, paths and the interpreter.
    env = {"SANDBOX_AGENT_SECRET": "local-secret", "SANDBOX_PROJECT_ID": PID, "SANDBOX_ENABLE_BACKEND": "false", "SANDBOX_TOPIC": "test"}
    sbx = SandboxSpec(project_id=PID, image="", env=env)
    driver = local_driver.LocalProcessDriver(s)
    info = await driver.start(sbx)
    try:
        deadline = time.monotonic() + 60
        async with httpx.AsyncClient() as client:
            while time.monotonic() < deadline:
                try:
                    r = await client.get(f"{info.endpoint}/status", headers={"authorization": "Bearer local-secret"})
                    if r.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await _sleep(0.3)
            else:
                pytest.fail((tmp_path / "sandboxes" / PID / "sandboxd.log").read_text()[-3000:])
            assert r.json()["project_id"] == PID
            assert (await client.get(f"{info.endpoint}/status")).status_code == 401
        workspace = tmp_path / "sandboxes" / PID / "workspace"
        assert (workspace / ".git").exists()  # bootstrapped like the upstream extension does

        # A new control-plane process finds the running sandbox through its pid file.
        reborn = local_driver.LocalProcessDriver(s)
        assert (await reborn.inspect(PID)).state == "running"
        await reborn.stop(PID)
        assert (await reborn.inspect(PID)).state == "stopped"
        pid = int(info.ref)
        assert not _alive(pid)
    finally:
        await driver.stop(PID)


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return Path(f"/proc/{pid}/cmdline").exists() and b"sandboxd" in Path(f"/proc/{pid}/cmdline").read_bytes()
