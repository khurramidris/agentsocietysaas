# syntax=docker/dockerfile:1.7
#
# Sandbox image: one AgentSociety research workspace per container (Docker driver)
# or pod (Kubernetes driver). Everything a researcher gets from a local install of
# the AgentSociety repository and VS Code extension:
#
#   /opt/agentsociety/venv        agentsociety2 from the upstream lockfile (engine,
#                                 backend, research tools, analysis extras)
#   /opt/agentsociety/extension   research skills (patched for the platform),
#                                 .agentsociety runtime launcher, bundled plugins
#   /opt/sandboxd/venv            the sandbox agent, isolated from the engine
#   claude                        Claude Code, driving the AI social scientist
#   code-server (optional)        VS Code in the browser
#   LaTeX/pandoc (optional)       paper writing toolchain
#
# Build from the repository root:
#   docker build -f deploy/docker/sandbox.Dockerfile -t agentsociety-saas/sandbox:dev .
# Options (build args): WITH_LATEX, WITH_IDE, WITH_TORCH (CPU wheels), CODE_SERVER_VERSION,
# CLAUDE_CODE_VERSION. Behind a TLS-intercepting proxy, pass its CA bundle:
#   --secret id=ca,src=/path/to/ca-bundle.crt

ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=22

FROM ghcr.io/astral-sh/uv:0.11.32 AS uv

# ---------------------------------------------------------------- Claude Code
FROM node:${NODE_VERSION}-bookworm-slim AS claude
ARG CLAUDE_CODE_VERSION=2.1.312
RUN --mount=type=secret,id=ca \
    if [ -s /run/secrets/ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/ca; fi; \
    npm install -g --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && claude --version

# ------------------------------------------------------------ research assets
# Patched copy of the extension's skills/runtime/plugins (scripts/prepare_sandbox_assets.sh).
FROM python:${PYTHON_VERSION}-slim-bookworm AS assets
COPY deploy/docker/apt-install.sh /usr/local/bin/apt-install
RUN --mount=type=secret,id=ca apt-install patch
WORKDIR /src
COPY scripts/prepare_sandbox_assets.sh scripts/apply_upstream_patches.sh scripts/
COPY upstream/patches/ upstream/patches/
COPY upstream/agentsociety/extension/skills/ upstream/agentsociety/extension/skills/
COPY upstream/agentsociety/extension/runtime/ upstream/agentsociety/extension/runtime/
COPY upstream/agentsociety/extension/plugins/ upstream/agentsociety/extension/plugins/
RUN bash scripts/prepare_sandbox_assets.sh /out/extension

# ---------------------------------------------------------------------- image
FROM python:${PYTHON_VERSION}-slim-bookworm

ARG WITH_LATEX=true
ARG WITH_IDE=true
ARG WITH_TORCH=false
ARG CODE_SERVER_VERSION=4.141.0

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY deploy/docker/apt-install.sh /usr/local/bin/apt-install
RUN --mount=type=secret,id=ca set -eux; \
    pkgs="ca-certificates curl git less procps ripgrep tini unzip"; \
    if [ "$WITH_LATEX" = "true" ]; then \
      pkgs="$pkgs biber latexmk pandoc poppler-utils texlive-bibtex-extra texlive-fonts-recommended texlive-latex-extra texlive-latex-recommended texlive-science"; \
    fi; \
    apt-install $pkgs

COPY --from=uv /uv /uvx /usr/local/bin/

# Node runtime + Claude Code (the package ships its own launcher under bin/).
COPY --from=claude /usr/local/bin/node /usr/local/bin/node
COPY --from=claude /usr/local/lib/node_modules/@anthropic-ai /usr/local/lib/node_modules/@anthropic-ai
RUN set -eux; \
    bin="$(node -p 'const p=require("/usr/local/lib/node_modules/@anthropic-ai/claude-code/package.json"); typeof p.bin==="string"?p.bin:p.bin.claude')"; \
    ln -s "/usr/local/lib/node_modules/@anthropic-ai/claude-code/${bin}" /usr/local/bin/claude; \
    claude --version

# agentsociety2: third-party dependencies exactly as locked upstream (hash-checked),
# then the vendored package itself without dependencies.
COPY upstream/agentsociety/pyproject.toml upstream/agentsociety/uv.lock /opt/agentsociety/src/
COPY upstream/agentsociety/packages/agentsociety2/pyproject.toml /opt/agentsociety/src/packages/agentsociety2/
RUN --mount=type=secret,id=ca --mount=type=cache,target=/root/.cache/uv set -eux; \
    if [ -s /run/secrets/ca ]; then export SSL_CERT_FILE=/run/secrets/ca; fi; \
    cd /opt/agentsociety/src; \
    uv export --frozen --no-dev --package agentsociety2 --extra analysis --extra eda \
      --no-emit-package agentsociety2 --format requirements-txt -o /tmp/agentsociety2.txt; \
    torch="$(awk '/^name = "torch"$/ {getline; gsub(/"/, "", $3); print $3; exit}' uv.lock)"; \
    # Install outside the upstream project, whose uv config points at a regional mirror;
    # the lockfile itself resolves against PyPI.
    cd /; \
    uv venv --no-config /opt/agentsociety/venv --python /usr/local/bin/python3; \
    uv pip install --no-config --python /opt/agentsociety/venv/bin/python --index-url https://pypi.org/simple \
      --require-hashes -r /tmp/agentsociety2.txt; \
    if [ "$WITH_TORCH" = "true" ]; then \
      uv pip install --no-config --python /opt/agentsociety/venv/bin/python --index-url https://download.pytorch.org/whl/cpu "torch==${torch}"; \
    fi; \
    rm /tmp/agentsociety2.txt
COPY upstream/agentsociety/packages/agentsociety2/ /opt/agentsociety/src/packages/agentsociety2/
RUN --mount=type=secret,id=ca --mount=type=cache,target=/root/.cache/uv set -eux; \
    if [ -s /run/secrets/ca ]; then export SSL_CERT_FILE=/run/secrets/ca; fi; \
    uv pip install --no-config --python /opt/agentsociety/venv/bin/python --index-url https://pypi.org/simple \
      --no-deps /opt/agentsociety/src/packages/agentsociety2; \
    rm -rf /opt/agentsociety/src

# The sandbox agent in its own environment, so a broken engine never takes it down.
COPY services/sandbox/requirements.lock /opt/sandboxd/requirements.lock
RUN --mount=type=secret,id=ca --mount=type=cache,target=/root/.cache/uv set -eux; \
    if [ -s /run/secrets/ca ]; then export SSL_CERT_FILE=/run/secrets/ca; fi; \
    uv venv /opt/sandboxd/venv --python /usr/local/bin/python3; \
    uv pip install --python /opt/sandboxd/venv/bin/python --require-hashes -r /opt/sandboxd/requirements.lock
COPY services/sandbox/ /opt/sandboxd/src/
RUN --mount=type=secret,id=ca --mount=type=cache,target=/root/.cache/uv set -eux; \
    if [ -s /run/secrets/ca ]; then export SSL_CERT_FILE=/run/secrets/ca; fi; \
    uv pip install --python /opt/sandboxd/venv/bin/python --no-deps /opt/sandboxd/src; \
    rm -rf /opt/sandboxd/src

# Browser IDE (optional).
RUN --mount=type=secret,id=ca set -eux; \
    if [ "$WITH_IDE" = "true" ]; then \
      if [ -s /run/secrets/ca ]; then export CURL_CA_BUNDLE=/run/secrets/ca; fi; \
      arch="$(dpkg --print-architecture)"; \
      curl -fsSL "https://github.com/coder/code-server/releases/download/v${CODE_SERVER_VERSION}/code-server-${CODE_SERVER_VERSION}-linux-${arch}.tar.gz" \
        | tar -xz -C /opt; \
      mv "/opt/code-server-${CODE_SERVER_VERSION}-linux-${arch}" /opt/code-server; \
      ln -s /opt/code-server/bin/code-server /usr/local/bin/code-server; \
    fi

COPY --from=assets /out/extension /opt/agentsociety/extension

# Unprivileged user; /data is the project's volume (workspace + agent state).
RUN set -eux; \
    groupadd --gid 1000 researcher; \
    useradd --uid 1000 --gid 1000 --home-dir /data/state/home --no-create-home --shell /bin/bash researcher; \
    mkdir -p /data/workspace /data/state/home; \
    chown -R 1000:1000 /data

ENV PATH=/opt/agentsociety/venv/bin:/usr/local/bin:/usr/bin:/bin \
    HOME=/data/state/home \
    SANDBOX_PYTHON=/opt/agentsociety/venv/bin/python \
    SANDBOX_ASSETS_DIR=/opt/agentsociety/extension \
    SANDBOX_CLAUDE_BIN=/usr/local/bin/claude \
    SANDBOX_CODE_SERVER_BIN=/usr/local/bin/code-server \
    SANDBOX_WORKSPACE=/data/workspace \
    SANDBOX_STATE_DIR=/data/state \
    RAY_TMPDIR=/tmp/ray \
    MPLCONFIGDIR=/tmp/matplotlib

USER 1000:1000
WORKDIR /data/workspace
VOLUME ["/data"]
EXPOSE 7000
HEALTHCHECK --interval=20s --timeout=5s --start-period=60s CMD curl -fsS http://127.0.0.1:7000/health || exit 1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/sandboxd/venv/bin/python", "-m", "sandboxd"]
