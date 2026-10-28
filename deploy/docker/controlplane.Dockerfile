# syntax=docker/dockerfile:1.7
#
# Control plane image: REST API, sandbox orchestration, LLM gateway, literature
# search (REST + MCP), dataset hub, billing, and the web app it serves at "/".
#
# Build from the repository root:
#   docker build -f deploy/docker/controlplane.Dockerfile -t agentsociety-saas/controlplane:dev .
# Behind a TLS-intercepting proxy, pass its CA bundle: --secret id=ca,src=/path/to/ca-bundle.crt
#
# Run "controlplane migrate" as a release step when several replicas start at once
# (and set CP_AUTO_MIGRATE=false); a single instance migrates on start.

ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=22

FROM ghcr.io/astral-sh/uv:0.11.32 AS uv

# -------------------------------------------------------------------- web app
FROM node:${NODE_VERSION}-bookworm-slim AS web
WORKDIR /src
COPY package.json package-lock.json ./
COPY services/web/package.json services/web/
RUN --mount=type=secret,id=ca --mount=type=cache,target=/root/.npm \
    if [ -s /run/secrets/ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/ca; fi; \
    npm ci --no-fund --no-audit
COPY services/web/ services/web/
# The app embeds the upstream extension's React webviews unmodified (plus the
# patches in upstream/patches/web, applied in memory at build time).
COPY upstream/patches/web/ upstream/patches/web/
COPY upstream/agentsociety/extension/tsconfig.json upstream/agentsociety/extension/
COPY upstream/agentsociety/extension/src/ upstream/agentsociety/extension/src/
COPY upstream/agentsociety/extension/media/ upstream/agentsociety/extension/media/
RUN npm run build -w services/web

# ---------------------------------------------------------------------- image
FROM python:${PYTHON_VERSION}-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=uv /uv /usr/local/bin/uv
COPY services/control-plane/requirements.lock /opt/controlplane/requirements.lock
RUN --mount=type=secret,id=ca --mount=type=cache,target=/root/.cache/uv set -eux; \
    if [ -s /run/secrets/ca ]; then export SSL_CERT_FILE=/run/secrets/ca; fi; \
    uv venv /opt/controlplane/venv --python /usr/local/bin/python3; \
    uv pip install --python /opt/controlplane/venv/bin/python --require-hashes -r /opt/controlplane/requirements.lock
COPY services/control-plane/ /opt/controlplane/src/
RUN --mount=type=secret,id=ca --mount=type=cache,target=/root/.cache/uv set -eux; \
    if [ -s /run/secrets/ca ]; then export SSL_CERT_FILE=/run/secrets/ca; fi; \
    uv pip install --python /opt/controlplane/venv/bin/python --no-deps /opt/controlplane/src; \
    rm -rf /opt/controlplane/src /usr/local/bin/uv

COPY --from=web /src/services/web/dist /opt/controlplane/web
COPY NOTICE /opt/controlplane/NOTICE

RUN set -eux; \
    groupadd --gid 10001 controlplane; \
    useradd --uid 10001 --gid 10001 --home-dir /data --no-create-home --shell /usr/sbin/nologin controlplane; \
    mkdir -p /data; \
    chown 10001:10001 /data

ENV PATH=/opt/controlplane/venv/bin:$PATH \
    CP_WEB_DIST_DIR=/opt/controlplane/web \
    CP_DATA_DIR=/data

USER 10001:10001
WORKDIR /data
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s \
  CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status != 200)"
ENTRYPOINT ["controlplane"]
CMD ["serve"]
