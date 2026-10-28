# Deploying the platform

Two supported topologies, both built from the same two images:

| Image | Built from | Contents |
| --- | --- | --- |
| `controlplane` | `deploy/docker/controlplane.Dockerfile` | API, sandbox orchestration, LLM gateway, literature search (REST + MCP), dataset hub, billing, and the web app |
| `sandbox` | `deploy/docker/sandbox.Dockerfile` | One research workspace: agentsociety2 (from the upstream lockfile), the research skills, Claude Code, the sandbox agent, optional code-server and LaTeX |

Both Dockerfiles take a BuildKit secret named `ca` for builds behind a TLS-intercepting
proxy (`--secret id=ca,src=ca-bundle.crt`); nothing from it ends up in the image.
Python dependencies are installed from hash-locked files
(`services/*/requirements.lock`, the upstream `uv.lock`), so a build either gets exactly
the tested packages or fails.

## Single host: Docker Compose

For a pilot or a small team. One Linux VM runs PostgreSQL, the control plane, Caddy
(TLS) and every sandbox container.

```bash
cd deploy/docker
cp .env.example .env                      # fill in; see the comments
mkdir -p config && cp ../model_catalog.example.json config/model_catalog.json
#   ...and fill in the prices in config/model_catalog.json
docker compose --profile build build      # control plane + sandbox images
sudo ./harden-host.sh                     # sandbox firewall; read it first
docker compose up -d
```

Then sign in with `CP_BOOTSTRAP_ADMIN_EMAIL` and open **Admin** to check models and
prices. Point Stripe's webhook at `https://<DOMAIN>/api/v1/billing/stripe/webhook`.

Security notes for this topology:

* The control plane talks to the host's Docker engine. That is root-equivalent, so the
  VM must be dedicated to the platform.
* Install [gVisor](https://gvisor.dev/docs/user_guide/install/) and set
  `CP_DOCKER_RUNTIME=runsc` so researcher code runs against a user-space kernel.
* `harden-host.sh` blocks sandbox traffic to private networks, the host and cloud
  metadata. Every sandbox bridge is named `sbx-...`, so the rules also cover projects
  created later; make them persistent (iptables-persistent or a unit file).
* Sandboxes get their own bridge network each (`CP_DOCKER_ACCESS=network`); they can
  reach the control plane but not each other.

## Kubernetes

For production. The control plane runs in namespace `agentsociety`; each project gets a
Pod and a PersistentVolumeClaim in `agentsociety-sandboxes`.

```bash
cd deploy/k8s
cp secrets.env.example secrets.env        # fill in (or use your secret store)
$EDITOR config.env ingress.yaml           # hosts, image, storage class, S3
kubectl apply -f optional/runtimeclass-gvisor.yaml   # if your sandbox nodes run gVisor
kubectl apply -k .
# optional: a model catalog
kubectl -n agentsociety create configmap model-catalog --from-file=model_catalog.json=../model_catalog.example.json
#   ...with prices filled in, and CP_MODEL_CATALOG_FILE=/config/model_catalog.json in config.env
```

What the manifests enforce:

* Both namespaces use the **restricted** Pod Security Standard. Sandbox pods run as
  UID 1000 with a read-only root filesystem, no capabilities, `RuntimeDefault` seccomp,
  no service-account token and no service links (see
  `controlplane/sandboxes/kubernetes.py`).
* The control plane's service account may only create, read and delete Pods and PVCs in
  the sandbox namespace.
* **NetworkPolicies** (needs a CNI that enforces them, e.g. Calico or Cilium): sandboxes
  accept connections only from the control plane on port 7000; they may resolve DNS,
  call the control plane on port 8000 and reach the public internet, but not private
  ranges, the node or the metadata endpoint. Adjust the `ingress-nginx` namespace name
  in `network-policies.yaml` to your ingress controller.
* A ResourceQuota caps the whole sandbox namespace; per-organization limits come from
  the plans in `controlplane/billing.py`.

Use a managed PostgreSQL (`optional/postgres.yaml` is for evaluation) and S3-compatible
storage for dataset packages. The bucket needs CORS allowing `PUT` from the app origin,
because browsers upload dataset parts directly with pre-signed URLs.

## Configuration reference

All settings are environment variables with the `CP_` prefix; see
`services/control-plane/controlplane/settings.py`. In production
(`CP_ENVIRONMENT=production`) the control plane refuses to start with a development
secret, without an encryption key, with the local sandbox driver, with the mock model
provider or with SQLite.

## Operations

* **Migrations** run on start for a single instance. When several processes start at
  once, run `controlplane migrate` first and set `CP_AUTO_MIGRATE=false` (the Kubernetes
  Deployment does this with an init container).
* **Scaling.** One control plane replica handles many sandboxes: it proxies requests
  and streams, while simulations run in the sandboxes. It keeps per-process state (the
  idle-sandbox reaper, the usage buffer, login rate limits), so scale it vertically, or
  move that state to PostgreSQL/Redis before running several replicas.
* **Backups.** PostgreSQL holds accounts, billing and usage; workspaces live on the
  sandbox volumes (Docker named volumes or PVCs); dataset packages in the blob store.
  Back up all three.
* **Idle sandboxes** stop after the plan's idle timeout (files are kept on the volume)
  and start again on the next request. Admins can run the reaper from **Admin**.
* **Upstream updates.** `scripts/sync_upstream.py` refreshes `upstream/agentsociety`;
  `scripts/apply_upstream_patches.sh` and the web build fail loudly if a platform patch
  in `upstream/patches` no longer applies. Rebuild both images afterwards.
