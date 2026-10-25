"""Control-plane configuration (environment variables, ``CP_`` prefix)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CP_", env_file=".env", extra="ignore")

    # ---- identity / URLs -------------------------------------------------
    brand_name: str = "Societas"
    environment: Literal["development", "test", "production"] = "development"
    # Public URL of the web app + API (what browsers use).
    public_base_url: str = "http://localhost:8000"
    # Origin that serves browser IDE sessions. Use a different registrable domain in
    # production so workspace code (IDE webviews) can never run on the app origin.
    ide_base_url: str = ""
    # URL sandboxes use to reach the control plane (gateway, MCP, dataset hub).
    internal_base_url: str = "http://localhost:8000"
    cors_origins: list[str] = Field(default_factory=list)

    # ---- secrets ---------------------------------------------------------
    secret_key: str = "dev-insecure-change-me"
    # Fernet key (urlsafe base64, 32 bytes) for encrypting provider API keys at rest.
    encryption_key: str = ""

    # Built web app (services/web/dist) served at "/"; empty disables.
    web_dist_dir: str = ""
    # Public (pk.*) Mapbox token for the replay map; maps fall back to a placeholder without it.
    mapbox_token: str = ""

    # ---- storage ---------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./.data/controlplane.db"
    # Apply database migrations on start (disable when replicas start concurrently and
    # run ``python -m controlplane migrate`` as a release step instead).
    auto_migrate: bool = True
    data_dir: Path = Path("./.data")
    blob_backend: Literal["local", "s3"] = "local"
    s3_bucket: str = ""
    s3_endpoint_url: str = ""
    s3_region: str = ""
    s3_prefix: str = "blobs/"

    # ---- auth ------------------------------------------------------------
    access_token_ttl_s: int = 15 * 60
    refresh_token_ttl_s: int = 30 * 24 * 3600
    allow_signup: bool = True
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""

    # ---- sandboxes -------------------------------------------------------
    sandbox_driver: Literal["local", "docker", "kubernetes"] = "local"
    sandbox_image: str = "agentsociety-saas/sandbox:latest"
    sandbox_idle_stop_s: int = 30 * 60
    sandbox_start_timeout_s: int = 180
    sandbox_reaper_interval_s: int = 60
    sandbox_enable_ide: bool = True
    # local driver (development only: no isolation between projects!)
    local_sandbox_python: str = ""
    local_sandbox_assets_dir: Path = Path("./.data/assets/extension")
    local_sandbox_port_range: tuple[int, int] = (17000, 17999)
    allow_insecure_local_sandboxes: bool = False
    # docker driver
    # "network": the control plane runs in a container; every sandbox gets its own
    #   bridge network that the control plane joins (sandboxes cannot reach each other).
    # "port": the control plane runs on the host; sandboxes share one bridge network with
    #   inter-container traffic disabled and publish their agent port on 127.0.0.1.
    docker_access: Literal["network", "port"] = "network"
    docker_network: str = "agentsociety-sandboxes"  # shared network for "port" access
    # Control-plane container to attach to sandbox networks ("network" access); defaults
    # to $HOSTNAME, which Docker sets to the container id.
    docker_control_plane_container: str = ""
    # DNS name of the control plane inside sandbox networks (match CP_INTERNAL_BASE_URL).
    docker_control_plane_alias: str = "controlplane"
    docker_runtime: str = ""  # e.g. "runsc" for gVisor
    # kubernetes driver
    k8s_namespace: str = "agentsociety-sandboxes"
    k8s_storage_class: str = ""
    k8s_runtime_class: str = ""  # e.g. "gvisor"
    k8s_volume_size: str = "10Gi"

    # ---- LLM gateway -----------------------------------------------------
    # Default models written into each sandbox's managed .env.
    default_sim_model: str = "gpt-5.5"
    default_coder_model: str = ""
    default_embedding_model: str = "text-embedding-3-large"
    default_embedding_dims: int = 1024
    default_chat_model: str = "claude-opus-5-5"
    default_chat_small_model: str = "claude-haiku-5-5"
    # Seeds platform providers on first start when set.
    openai_api_key: str = ""
    openai_api_base: str = "https://api.openai.com/v1"
    anthropic_api_key: str = ""
    anthropic_api_base: str = "https://api.anthropic.com"
    # Development/demo: serve every unknown model from the deterministic mock provider.
    enable_mock_provider: bool = False
    # Operator-maintained providers/models/prices (JSON), see deploy/model_catalog.example.json.
    model_catalog_file: str = ""
    gateway_timeout_s: float = 600.0
    usage_flush_interval_s: float = 2.0
    # Multiplier from provider list price to customer price when seeding the catalog.
    price_multiplier: float = 1.0
    # Let organisations point their own (BYOK) providers at private/internal addresses.
    # Off by default: the gateway would otherwise fetch internal URLs on their behalf.
    allow_private_provider_urls: bool = False

    # ---- billing ---------------------------------------------------------
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_pro: str = ""
    stripe_price_team: str = ""
    signup_credit_usd: float = 5.0
    # Compute pricing in USD per sandbox-hour by size.
    sandbox_hourly_usd: dict[str, float] = Field(default_factory=lambda: {"small": 0.06, "medium": 0.12, "large": 0.24, "xlarge": 0.48})

    # ---- literature search -----------------------------------------------
    openalex_mailto: str = ""
    literature_sources: list[str] = Field(default_factory=lambda: ["openalex", "arxiv", "crossref", "local"])
    literature_timeout_s: float = 20.0

    @field_validator("public_base_url", "internal_base_url", "ide_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def effective_ide_base_url(self) -> str:
        return self.ide_base_url or self.public_base_url

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def validate_for_production(self) -> list[str]:
        problems = []
        if self.secret_key.startswith("dev-") or len(self.secret_key) < 32:
            problems.append("CP_SECRET_KEY must be a random string of at least 32 characters")
        if not self.encryption_key:
            problems.append("CP_ENCRYPTION_KEY must be set (Fernet key)")
        if self.sandbox_driver == "local":
            problems.append("CP_SANDBOX_DRIVER=local offers no isolation; use docker or kubernetes")
        if self.enable_mock_provider:
            problems.append("CP_ENABLE_MOCK_PROVIDER must be false in production")
        if self.database_url.startswith("sqlite"):
            problems.append("Use PostgreSQL (CP_DATABASE_URL=postgresql+asyncpg://...) in production")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
