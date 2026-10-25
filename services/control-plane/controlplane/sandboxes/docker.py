"""Docker sandboxes: one hardened container + one named volume per project.

The container is re-created on every start (the volume keeps ``/data/workspace``
and ``/data/state``), so a rotated project token or a new image takes effect
immediately. Hardening: all capabilities dropped, no-new-privileges, pid and
memory limits, non-root user from the image, optional gVisor runtime.

Tenant isolation on the network: code in a sandbox (Ray's GCS, a server the
researcher starts...) must never be reachable from another tenant's sandbox.

* ``network`` access - each project gets its own bridge network; the control
  plane container joins it under ``CP_DOCKER_CONTROL_PLANE_ALIAS`` and reaches
  the sandbox by container name.
* ``port`` access - for a control plane running on the host: sandboxes share one
  bridge network with inter-container communication disabled and publish the
  agent port on ``127.0.0.1`` only; they reach the host as ``host.docker.internal``.

Either way, block the cloud metadata endpoint and other host-local services from
sandbox networks on the host firewall (see deploy/docker/harden-host.sh).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

from ..settings import Settings
from .base import AGENT_PORT, SandboxDriver, SandboxInfo, SandboxSpec

LABEL_MANAGED = "agentsociety.managed"
LABEL_PROJECT = "agentsociety.project"


def network_name(project_id: str) -> str:
    return f"sbxnet-{project_id.replace('_', '-').lower()}"


class DockerDriver(SandboxDriver):
    name = "docker"

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        if client is None:
            import docker  # optional dependency: controlplane[docker]

            client = docker.from_env()
        self.client = client
        self._shared_network_ready = False

    # -- helpers ---------------------------------------------------------------

    def _container_name(self, project_id: str) -> str:
        return SandboxSpec(project_id=project_id, image="", env={}).name

    def _volume_name(self, project_id: str) -> str:
        return SandboxSpec(project_id=project_id, image="", env={}).volume

    @staticmethod
    def _not_found(exc: Exception) -> bool:
        return type(exc).__name__ == "NotFound" or getattr(getattr(exc, "response", None), "status_code", None) == 404

    def _get(self, name: str):
        try:
            return self.client.containers.get(name)
        except Exception as exc:
            if self._not_found(exc):
                return None
            raise

    def _control_plane_container(self) -> str:
        return self.settings.docker_control_plane_container or os.environ.get("HOSTNAME", "")

    def _ensure_shared_network(self) -> str:
        """The ICC-disabled network used with ``port`` access."""
        name = self.settings.docker_network
        if not self._shared_network_ready:
            try:
                self.client.networks.get(name)
            except Exception as exc:
                if not self._not_found(exc):
                    raise
                self.client.networks.create(
                    name,
                    driver="bridge",
                    options={"com.docker.network.bridge.enable_icc": "false"},
                    labels={LABEL_MANAGED: "true"},
                )
            self._shared_network_ready = True
        return name

    def _ensure_project_network(self, project_id: str) -> str:
        """A private bridge network for one sandbox, joined by the control plane."""
        name = network_name(project_id)
        try:
            network = self.client.networks.get(name)
        except Exception as exc:
            if not self._not_found(exc):
                raise
            network = self.client.networks.create(name, driver="bridge", labels={LABEL_MANAGED: "true", LABEL_PROJECT: project_id})
        me = self._control_plane_container()
        if not me:
            raise RuntimeError(
                "CP_DOCKER_ACCESS=network needs the control plane to run in a container (set CP_DOCKER_CONTROL_PLANE_CONTAINER) - or use CP_DOCKER_ACCESS=port"
            )
        try:
            network.connect(me, aliases=[self.settings.docker_control_plane_alias])
        except Exception as exc:
            if "already exists" not in str(exc):  # already attached: nothing to do
                raise
        return name

    def _remove_project_network(self, project_id: str) -> None:
        try:
            network = self.client.networks.get(network_name(project_id))
        except Exception as exc:
            if self._not_found(exc):
                return
            raise
        me = self._control_plane_container()
        if me:
            with contextlib.suppress(Exception):
                network.disconnect(me, force=True)
        with contextlib.suppress(Exception):
            network.remove()

    def _endpoint(self, container) -> str | None:
        if self.settings.docker_access == "network":
            return f"http://{container.name}:{AGENT_PORT}"
        container.reload()
        ports = (container.attrs.get("NetworkSettings") or {}).get("Ports") or {}
        bindings = ports.get(f"{AGENT_PORT}/tcp") or []
        if not bindings:
            return None
        return f"http://127.0.0.1:{bindings[0]['HostPort']}"

    def container_kwargs(self, spec: SandboxSpec, network: str) -> dict[str, Any]:
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
            "hostname": "sandbox",
            "detach": True,
            "environment": env,
            "labels": {LABEL_MANAGED: "true", LABEL_PROJECT: spec.project_id, **spec.labels},
            "volumes": {spec.volume: {"bind": "/data", "mode": "rw"}},
            "network": network,
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
            kwargs["extra_hosts"] = {"host.docker.internal": "host-gateway"}
        return kwargs

    # -- driver API ------------------------------------------------------------

    async def start(self, spec: SandboxSpec) -> SandboxInfo:
        def _run() -> SandboxInfo:
            existing = self._get(spec.name)
            if existing is not None:
                existing.remove(force=True)
            network = self._ensure_project_network(spec.project_id) if self.settings.docker_access == "network" else self._ensure_shared_network()
            container = self.client.containers.run(**self.container_kwargs(spec, network))
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
            if self.settings.docker_access == "network":
                self._remove_project_network(project_id)

        await asyncio.to_thread(_stop)

    async def destroy(self, project_id: str, *, delete_data: bool) -> None:
        await self.stop(project_id)
        if delete_data:

            def _rm_volume() -> None:
                try:
                    self.client.volumes.get(self._volume_name(project_id)).remove(force=True)
                except Exception as exc:
                    if not self._not_found(exc):
                        raise

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
            return SandboxInfo(
                state="error", ref=container.id[:12], detail=f"{container.status}: exit {state.get('ExitCode')} {state.get('Error', '')}".strip()
            )

        return await asyncio.to_thread(_inspect)
