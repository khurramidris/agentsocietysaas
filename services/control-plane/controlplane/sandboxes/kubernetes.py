"""Kubernetes sandboxes: one Pod + one PersistentVolumeClaim per project.

Manifests are built by pure functions (``pvc_manifest`` / ``pod_manifest``) so they
can be reviewed and unit-tested without a cluster. Pods run non-root with a
read-only root filesystem, no service-account token, all capabilities dropped,
``RuntimeDefault`` seccomp and an optional sandboxed RuntimeClass (gVisor/Kata).
Network isolation is a namespace-level NetworkPolicy shipped in deploy/k8s.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..settings import Settings
from .base import AGENT_PORT, SandboxDriver, SandboxInfo, SandboxSpec

LABELS = {"app.kubernetes.io/part-of": "agentsociety-saas", "app.kubernetes.io/component": "sandbox"}


def pvc_manifest(settings: Settings, spec: SandboxSpec) -> dict[str, Any]:
    body: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": spec.volume, "labels": {**LABELS, "agentsociety/project": spec.project_id}},
        "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": settings.k8s_volume_size}}},
    }
    if settings.k8s_storage_class:
        body["spec"]["storageClassName"] = settings.k8s_storage_class
    return body


def pod_manifest(settings: Settings, spec: SandboxSpec) -> dict[str, Any]:
    env = {
        **spec.env,
        "SANDBOX_HOST": "0.0.0.0",
        "SANDBOX_PORT": str(AGENT_PORT),
        "SANDBOX_WORKSPACE": "/data/workspace",
        "SANDBOX_STATE_DIR": "/data/state",
    }
    memory = f"{int(spec.memory_gb * 1024)}Mi"
    pod: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": spec.name, "labels": {**LABELS, "agentsociety/project": spec.project_id, **spec.labels}},
        "spec": {
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "restartPolicy": "Always",
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "fsGroup": 1000,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "sandbox",
                    "image": spec.image,
                    "imagePullPolicy": "IfNotPresent",
                    "ports": [{"containerPort": AGENT_PORT, "name": "agent"}],
                    "env": [{"name": k, "value": v} for k, v in sorted(env.items())],
                    "resources": {
                        "requests": {"cpu": str(max(spec.cpus / 2, 0.25)), "memory": memory},
                        "limits": {"cpu": str(spec.cpus), "memory": memory},
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "readinessProbe": {"httpGet": {"path": "/health", "port": AGENT_PORT}, "periodSeconds": 5},
                    "livenessProbe": {
                        "httpGet": {"path": "/health", "port": AGENT_PORT},
                        "initialDelaySeconds": 30,
                        "periodSeconds": 20,
                    },
                    "volumeMounts": [
                        {"name": "data", "mountPath": "/data"},
                        {"name": "tmp", "mountPath": "/tmp"},
                        {"name": "shm", "mountPath": "/dev/shm"},
                    ],
                }
            ],
            "volumes": [
                {"name": "data", "persistentVolumeClaim": {"claimName": spec.volume}},
                {"name": "tmp", "emptyDir": {"sizeLimit": "4Gi"}},
                {"name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "2Gi"}},
            ],
        },
    }
    if settings.k8s_runtime_class:
        pod["spec"]["runtimeClassName"] = settings.k8s_runtime_class
    return pod


class KubernetesDriver(SandboxDriver):
    name = "kubernetes"

    def __init__(self, settings: Settings, core_api: Any | None = None) -> None:
        self.settings = settings
        self.ns = settings.k8s_namespace
        if core_api is None:
            from kubernetes import client, config  # optional dependency: controlplane[kubernetes]

            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            core_api = client.CoreV1Api()
        self.api = core_api

    @staticmethod
    def _is_not_found(exc: Exception) -> bool:
        return getattr(exc, "status", None) == 404

    def _names(self, project_id: str) -> tuple[str, str]:
        spec = SandboxSpec(project_id=project_id, image="", env={})
        return spec.name, spec.volume

    async def start(self, spec: SandboxSpec) -> SandboxInfo:
        def _run() -> SandboxInfo:
            try:
                self.api.read_namespaced_persistent_volume_claim(spec.volume, self.ns)
            except Exception as exc:
                if not self._is_not_found(exc):
                    raise
                self.api.create_namespaced_persistent_volume_claim(self.ns, pvc_manifest(self.settings, spec))
            try:
                self.api.delete_namespaced_pod(spec.name, self.ns, grace_period_seconds=10)
            except Exception as exc:
                if not self._is_not_found(exc):
                    raise
            # The old pod may still be terminating; retry creation until its name is free.
            for _ in range(60):
                try:
                    self.api.create_namespaced_pod(self.ns, pod_manifest(self.settings, spec))
                    break
                except Exception as exc:
                    if getattr(exc, "status", None) != 409:
                        raise
                    import time

                    time.sleep(1)
            return SandboxInfo(state="running", endpoint=None, ref=spec.name, detail="pending")

        await asyncio.to_thread(_run)
        return await self.inspect(spec.project_id)

    async def stop(self, project_id: str) -> None:
        pod, _ = self._names(project_id)

        def _stop() -> None:
            try:
                self.api.delete_namespaced_pod(pod, self.ns, grace_period_seconds=20)
            except Exception as exc:
                if not self._is_not_found(exc):
                    raise

        await asyncio.to_thread(_stop)

    async def destroy(self, project_id: str, *, delete_data: bool) -> None:
        await self.stop(project_id)
        if delete_data:
            _, pvc = self._names(project_id)

            def _rm() -> None:
                try:
                    self.api.delete_namespaced_persistent_volume_claim(pvc, self.ns)
                except Exception as exc:
                    if not self._is_not_found(exc):
                        raise

            await asyncio.to_thread(_rm)

    async def inspect(self, project_id: str) -> SandboxInfo:
        pod_name, _ = self._names(project_id)

        def _inspect() -> SandboxInfo:
            try:
                pod = self.api.read_namespaced_pod(pod_name, self.ns)
            except Exception as exc:
                if self._is_not_found(exc):
                    return SandboxInfo(state="stopped")
                raise
            phase = pod.status.phase if pod.status else "Unknown"
            ip = pod.status.pod_ip if pod.status else None
            if phase in ("Pending", "Running"):
                endpoint = f"http://{ip}:{AGENT_PORT}" if ip else None
                return SandboxInfo(state="running", endpoint=endpoint, ref=pod_name, detail=phase)
            return SandboxInfo(state="error", ref=pod_name, detail=f"pod phase {phase}")

        return await asyncio.to_thread(_inspect)
