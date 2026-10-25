"""Docker sandboxes: one hardened container + one named volume per project.

The container is re-created on every start (the volume keeps ``/data/workspace``
and ``/data/state``), so a rotated project token or a new image takes effect
immediately. Hardening: all capabilities dropped, no-new-privileges, pid and
memory limits, non-root user from the image, optional gVisor runtime.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..settings import Settings
from .base import AGENT_PORT, SandboxDriver, SandboxInfo, SandboxSpec

LABEL_MANAGED = "agentsociety.managed"
LABEL_PROJECT = "agentsociety.project"


class DockerDriver(SandboxDriver):
    name = "docker"

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        if client is None:
            import docker  # optional dependency: controlplane[docker]

            client = docker.from_env()
        self.client = client
        self._network_ready = False

    # -- helpers ---------------------------------------------------------------

    def _container_name(self, project_id: str) -> str:
        return SandboxSpec(project_id=project_id, image="", env={}).name

    def _volume_name(self, project_id: str) -> str:
        return SandboxSpec(project_id=project_id, image="", env={}).volume

    def _get(self, name: str):
        from docker.errors import NotFound

        try:
            return self.client.containers.get(name)
        except NotFound:
            return None

    def _ensure_network(self) -> None:
        if self._network_ready:
            return
        from docker.errors import NotFound

        try:
            self.client.networks.get(self.settings.docker_network)
        except NotFound:
            self.client.networks.create(self.settings.docker_network, driver="bridge", labels={LABEL_MANAGED: "true"})
        self._network_ready = True

    def _endpoint(self, container) -> str | None:
        if self.settings.docker_access == "network":
            return f"http://{container.name}:{AGENT_PORT}"
        container.reload()
        ports = (container.attrs.get("NetworkSettings") or {}).get("Ports") or {}
        bindings = ports.get(f"{AGENT_PORT}/tcp") or []
        if not bindings:
            return None
        return f"http://127.0.0.1:{bindings[0]['HostPort']}"

    def container_kwargs(self, spec: SandboxSpec) -> dict[str, Any]:
        """Arguments for ``containers.run`` (separate for testability)."""
        env = {
            **spec.env,
            "SANDBOX_HOST": "0.0.0.0",
            "SANDBOX_PORT": str(AGENT_PORT),
            "SANDBOX_WORKSPACE": "/data/workspace",
            "SANDBOX_STATE_DIR": "/data/state",
        }
        kwargs: dict[str, Any] = {
            "image": spec.image,
            "name": spec.name,
            "detach": True,
            "environment": env,
            "labels": {LABEL_MANAGED: "true", LABEL_PROJECT: spec.project_id, **spec.labels},
            "volumes": {spec.volume: {"bind": "/data", "mode": "rw"}},
            "network": self.settings.docker_network,
            "nano_cpus": int(spec.cpus * 1e9),
            "mem_limit": f"{int(spec.memory_gb * 1024)}m",
            "memswap_limit": f"{int(spec.memory_gb * 1024)}m",
            "shm_size": "2g",  # Ray object store
            "pids_limit": 4096,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "restart_policy": {"Name": "unless-stopped"},
        }
        if self.settings.docker_runtime:
            kwargs["runtime"] = self.settings.docker_runtime
        if self.settings.docker_access == "port":
            kwargs["ports"] = {f"{AGENT_PORT}/tcp": ("127.0.0.1", None)}
        return kwargs

    # -- driver API ------------------------------------------------------------

    async def start(self, spec: SandboxSpec) -> SandboxInfo:
        def _run() -> SandboxInfo:
            self._ensure_network()
            existing = self._get(spec.name)
            if existing is not None:
                existing.remove(force=True)
            container = self.client.containers.run(**self.container_kwargs(spec))
            return SandboxInfo(state="running", endpoint=self._endpoint(container), ref=container.id[:12])

        return await asyncio.to_thread(_run)

    async def stop(self, project_id: str) -> None:
        def _stop() -> None:
            container = self._get(self._container_name(project_id))
            if container is not None:
                try:
                    container.stop(timeout=20)
                finally:
                    container.remove(force=True)

        await asyncio.to_thread(_stop)

    async def destroy(self, project_id: str, *, delete_data: bool) -> None:
        await self.stop(project_id)
        if delete_data:
            from docker.errors import NotFound

            def _rm_volume() -> None:
                try:
                    self.client.volumes.get(self._volume_name(project_id)).remove(force=True)
                except NotFound:
                    pass

            await asyncio.to_thread(_rm_volume)

    async def inspect(self, project_id: str) -> SandboxInfo:
        def _inspect() -> SandboxInfo:
            container = self._get(self._container_name(project_id))
            if container is None:
                return SandboxInfo(state="stopped")
            container.reload()
            if container.status == "running":
                return SandboxInfo(state="running", endpoint=self._endpoint(container), ref=container.id[:12])
            if container.status in ("created", "restarting"):
                return SandboxInfo(state="running", endpoint=self._endpoint(container), ref=container.id[:12], detail=container.status)
            state = container.attrs.get("State") or {}
            return SandboxInfo(state="error", ref=container.id[:12], detail=f"{container.status}: exit {state.get('ExitCode')} {state.get('Error', '')}".strip())

        return await asyncio.to_thread(_inspect)
