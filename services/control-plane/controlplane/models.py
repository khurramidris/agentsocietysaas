"""ORM models of the control plane."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, new_id, utcnow

ROLES = ("viewer", "member", "admin", "owner")
ROLE_RANK = {role: i for i, role in enumerate(ROLES)}


def _pk(prefix: str):
    return mapped_column(String(32), primary_key=True, default=lambda: new_id(prefix))


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = _pk("usr")
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(default=None)

    memberships: Mapped[list[Membership]] = relationship(back_populates="user", cascade="all, delete-orphan")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[str] = _pk("rft")
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    user_agent: Mapped[str] = mapped_column(String(300), default="")


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = _pk("org")
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    plan: Mapped[str] = mapped_column(String(32), default="free")
    is_personal: Mapped[bool] = mapped_column(Boolean, default=False)
    # Prepaid credit in micro-USD. Authoritative balance; ledger + usage are the audit trail.
    balance_micros: Mapped[int] = mapped_column(default=0)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), default=None)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), default=None)
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    memberships: Mapped[list[Membership]] = relationship(back_populates="org", cascade="all, delete-orphan")


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("org_id", "user_id"),)
    id: Mapped[str] = _pk("mem")
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16), default="member")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    org: Mapped[Organization] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")


class Invitation(Base):
    __tablename__ = "invitations"
    id: Mapped[str] = _pk("inv")
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[str] = mapped_column(String(16), default="member")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    invited_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]
    accepted_at: Mapped[datetime | None] = mapped_column(default=None)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = _pk("key")
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)
    name: Mapped[str] = mapped_column(String(200))
    prefix: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    secret_hash: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16), default="member")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("org_id", "slug"),)
    id: Mapped[str] = _pk("prj")
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, default="")
    topic: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(default=None)
    # sim/coder/embedding/chat model overrides, sandbox size, IDE flag, env overrides
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)

    sandbox_status: Mapped[str] = mapped_column(String(16), default="stopped")
    sandbox_ref: Mapped[str | None] = mapped_column(String(128), default=None)
    sandbox_endpoint: Mapped[str | None] = mapped_column(String(255), default=None)
    sandbox_secret_enc: Mapped[str | None] = mapped_column(Text, default=None)
    sandbox_size: Mapped[str] = mapped_column(String(16), default="small")
    sandbox_started_at: Mapped[datetime | None] = mapped_column(default=None)
    sandbox_billed_until: Mapped[datetime | None] = mapped_column(default=None)
    sandbox_last_activity_at: Mapped[datetime | None] = mapped_column(default=None)
    sandbox_error: Mapped[str | None] = mapped_column(Text, default=None)


class ProjectToken(Base):
    """Credential handed to a sandbox for the gateway, literature MCP and dataset hub."""

    __tablename__ = "project_tokens"
    id: Mapped[str] = _pk("ptk")
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    secret_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)


class Provider(Base):
    __tablename__ = "providers"
    id: Mapped[str] = _pk("prv")
    org_id: Mapped[str | None] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True, default=None)
    name: Mapped[str] = mapped_column(String(120))
    # openai: OpenAI-compatible /chat/completions + /embeddings (OpenAI, DeepSeek, OpenRouter, vLLM ...)
    # anthropic: Anthropic Messages API (also Anthropic-compatible gateways)
    # mock: deterministic built-in model for tests and demos
    kind: Mapped[str] = mapped_column(String(32))
    api_base: Mapped[str] = mapped_column(String(500), default="")
    api_key_enc: Mapped[str | None] = mapped_column(Text, default=None)
    extra_headers: Mapped[dict[str, Any]] = mapped_column(default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ModelRoute(Base):
    """A model name clients may request, the provider that serves it, and its price."""

    __tablename__ = "model_routes"
    __table_args__ = (UniqueConstraint("org_id", "alias"),)
    id: Mapped[str] = _pk("mdl")
    org_id: Mapped[str | None] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True, default=None)
    alias: Mapped[str] = mapped_column(String(160), index=True)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"))
    upstream_model: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16), default="chat")  # chat | embedding
    description: Mapped[str] = mapped_column(String(500), default="")
    # Customer prices in nano-USD per token (1 USD/MTok == 1000).
    input_nanos: Mapped[int] = mapped_column(default=0)
    output_nanos: Mapped[int] = mapped_column(default=0)
    cache_read_nanos: Mapped[int] = mapped_column(default=0)
    cache_write_nanos: Mapped[int] = mapped_column(default=0)
    cache_write_1h_nanos: Mapped[int] = mapped_column(default=0)
    # Long-context tier: when the prompt exceeds this many tokens every price is
    # multiplied by long_context_multiplier (e.g. Claude Haiku 5.5 above 100K tokens).
    long_context_tokens: Mapped[int] = mapped_column(default=0)
    long_context_multiplier: Mapped[float] = mapped_column(default=1.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class UsageEvent(Base):
    __tablename__ = "usage_events"
    __table_args__ = (Index("ix_usage_org_time", "org_id", "created_at"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String(32))
    project_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    api_key_id: Mapped[str | None] = mapped_column(String(32), default=None)
    source: Mapped[str] = mapped_column(String(16), default="sandbox")
    kind: Mapped[str] = mapped_column(String(16), default="llm")  # llm | embedding | compute
    model: Mapped[str] = mapped_column(String(160), default="")
    provider_id: Mapped[str | None] = mapped_column(String(32), default=None)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cached_tokens: Mapped[int] = mapped_column(default=0)
    cache_write_tokens: Mapped[int] = mapped_column(default=0)
    quantity: Mapped[int] = mapped_column(default=0)  # compute: seconds
    cost_micros: Mapped[int] = mapped_column(default=0)
    latency_ms: Mapped[int] = mapped_column(default=0)
    status_code: Mapped[int] = mapped_column(default=200)
    request_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class CreditLedger(Base):
    __tablename__ = "credit_ledger"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    delta_micros: Mapped[int]
    kind: Mapped[str] = mapped_column(String(24))  # grant | topup | subscription | refund | adjustment
    description: Mapped[str] = mapped_column(String(500), default="")
    # Idempotency key, e.g. a Stripe event id; unique when present.
    ref: Mapped[str | None] = mapped_column(String(128), unique=True, default=None)
    created_by: Mapped[str | None] = mapped_column(String(32), default=None)
    balance_after: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)  # slug used by the skills
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(32), default=None)
    name: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(40), default="other")
    version: Mapped[str] = mapped_column(String(40), default="1.0.0")
    tags: Mapped[list[str]] = mapped_column(default=list)
    author: Mapped[str] = mapped_column(String(300), default="")
    license: Mapped[str] = mapped_column(String(120), default="CC BY 4.0")
    visibility: Mapped[str] = mapped_column(String(16), default="org")  # org | public
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|uploaded|pending_review|published|rejected
    package_key: Mapped[str | None] = mapped_column(String(300), default=None)
    package_size_bytes: Mapped[int | None] = mapped_column(default=None)
    readme: Mapped[str] = mapped_column(Text, default="")
    file_tree: Mapped[dict[str, Any]] = mapped_column(default=dict)
    review_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(default=None)


class DatasetUpload(Base):
    __tablename__ = "dataset_uploads"
    id: Mapped[str] = _pk("upl")
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    part_size: Mapped[int]
    total_size: Mapped[int]
    parts: Mapped[int]
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class LiteratureDoc(Base):
    """Organisation-private texts ingested for the literature search 'local' source."""

    __tablename__ = "literature_docs"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(1000))
    text: Mapped[str] = mapped_column(Text)
    doi: Mapped[str] = mapped_column(String(300), default="")
    url: Mapped[str] = mapped_column(String(1000), default="")
    year: Mapped[int | None] = mapped_column(default=None)
    authors: Mapped[list[str]] = mapped_column(default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_org_time", "org_id", "created_at"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    org_id: Mapped[str | None] = mapped_column(String(32), default=None)
    user_id: Mapped[str | None] = mapped_column(String(32), default=None)
    action: Mapped[str] = mapped_column(String(80))
    target_type: Mapped[str] = mapped_column(String(40), default="")
    target_id: Mapped[str] = mapped_column(String(100), default="")
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
