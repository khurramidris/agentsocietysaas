"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.String(length=32), nullable=True),
        sa.Column("user_id", sa.String(length=32), nullable=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("target_type", sa.String(length=40), nullable=False),
        sa.Column("target_id", sa.String(length=100), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.create_index("ix_audit_org_time", ["org_id", "created_at"], unique=False)

    op.create_table(
        "organizations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("plan", sa.String(length=32), nullable=False),
        sa.Column("is_personal", sa.Boolean(), nullable=False),
        sa.Column("balance_micros", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("stripe_customer_id", sa.String(length=64), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(length=64), nullable=True),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
    )
    with op.batch_alter_table("organizations", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_organizations_slug"), ["slug"], unique=True)

    op.create_table(
        "usage_events",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=True),
        sa.Column("api_key_id", sa.String(length=32), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column("provider_id", sa.String(length=32), nullable=True),
        sa.Column("input_tokens", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("output_tokens", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("cached_tokens", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("cache_write_tokens", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("quantity", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("cost_micros", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("latency_ms", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("status_code", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_events")),
    )
    with op.batch_alter_table("usage_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_usage_events_project_id"), ["project_id"], unique=False)
        batch_op.create_index("ix_usage_org_time", ["org_id", "created_at"], unique=False)

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_platform_admin", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_users_email"), ["email"], unique=True)

    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("org_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("prefix", sa.String(length=32), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_api_keys_org_id_organizations"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_api_keys_user_id_users"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
    )
    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_api_keys_org_id"), ["org_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_api_keys_prefix"), ["prefix"], unique=True)

    op.create_table(
        "credit_ledger",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.String(length=32), nullable=False),
        sa.Column("delta_micros", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("ref", sa.String(length=128), nullable=True),
        sa.Column("created_by", sa.String(length=32), nullable=True),
        sa.Column("balance_after", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_credit_ledger_org_id_organizations"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credit_ledger")),
        sa.UniqueConstraint("ref", name=op.f("uq_credit_ledger_ref")),
    )
    with op.batch_alter_table("credit_ledger", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_credit_ledger_org_id"), ["org_id"], unique=False)

    op.create_table(
        "datasets",
        sa.Column("id", sa.String(length=100), nullable=False),
        sa.Column("org_id", sa.String(length=32), nullable=False),
        sa.Column("owner_user_id", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("version", sa.String(length=40), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("author", sa.String(length=300), nullable=False),
        sa.Column("license", sa.String(length=120), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("package_key", sa.String(length=300), nullable=True),
        sa.Column("package_size_bytes", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("readme", sa.Text(), nullable=False),
        sa.Column("file_tree", sa.JSON(), nullable=False),
        sa.Column("review_note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_datasets_org_id_organizations"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_datasets")),
    )
    with op.batch_alter_table("datasets", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_datasets_org_id"), ["org_id"], unique=False)

    op.create_table(
        "invitations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("org_id", sa.String(length=32), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("invited_by", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"], name=op.f("fk_invitations_invited_by_users"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_invitations_org_id_organizations"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invitations")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_invitations_token_hash")),
    )
    with op.batch_alter_table("invitations", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_invitations_email"), ["email"], unique=False)
        batch_op.create_index(batch_op.f("ix_invitations_org_id"), ["org_id"], unique=False)

    op.create_table(
        "literature_docs",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=1000), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("doi", sa.String(length=300), nullable=False),
        sa.Column("url", sa.String(length=1000), nullable=False),
        sa.Column("year", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("authors", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_literature_docs_org_id_organizations"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_literature_docs")),
    )
    with op.batch_alter_table("literature_docs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_literature_docs_org_id"), ["org_id"], unique=False)

    op.create_table(
        "memberships",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("org_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_memberships_org_id_organizations"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_memberships_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memberships")),
        sa.UniqueConstraint("org_id", "user_id", name=op.f("uq_memberships_org_id")),
    )
    with op.batch_alter_table("memberships", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_memberships_org_id"), ["org_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_memberships_user_id"), ["user_id"], unique=False)

    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("org_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("sandbox_status", sa.String(length=16), nullable=False),
        sa.Column("sandbox_ref", sa.String(length=128), nullable=True),
        sa.Column("sandbox_endpoint", sa.String(length=255), nullable=True),
        sa.Column("sandbox_secret_enc", sa.Text(), nullable=True),
        sa.Column("sandbox_size", sa.String(length=16), nullable=False),
        sa.Column("sandbox_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sandbox_billed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sandbox_last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sandbox_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=op.f("fk_projects_created_by_users"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_projects_org_id_organizations"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
        sa.UniqueConstraint("org_id", "slug", name=op.f("uq_projects_org_id")),
    )
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_projects_org_id"), ["org_id"], unique=False)

    op.create_table(
        "providers",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("org_id", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("api_base", sa.String(length=500), nullable=False),
        sa.Column("api_key_enc", sa.Text(), nullable=True),
        sa.Column("extra_headers", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_providers_org_id_organizations"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_providers")),
    )
    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_providers_org_id"), ["org_id"], unique=False)

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(length=300), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_refresh_tokens_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_refresh_tokens_token_hash")),
    )
    with op.batch_alter_table("refresh_tokens", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_refresh_tokens_user_id"), ["user_id"], unique=False)

    op.create_table(
        "dataset_uploads",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("dataset_id", sa.String(length=100), nullable=False),
        sa.Column("part_size", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("total_size", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("parts", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("backend_upload_id", sa.String(length=1024), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.id"], name=op.f("fk_dataset_uploads_dataset_id_datasets"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_uploads")),
    )
    with op.batch_alter_table("dataset_uploads", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_dataset_uploads_dataset_id"), ["dataset_id"], unique=False)

    op.create_table(
        "model_routes",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("org_id", sa.String(length=32), nullable=True),
        sa.Column("alias", sa.String(length=160), nullable=False),
        sa.Column("provider_id", sa.String(length=32), nullable=False),
        sa.Column("upstream_model", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("input_nanos", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("output_nanos", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("cache_read_nanos", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("cache_write_nanos", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("cache_write_1h_nanos", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("long_context_tokens", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("long_context_multiplier", sa.Double(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_model_routes_org_id_organizations"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"], name=op.f("fk_model_routes_provider_id_providers"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_routes")),
        sa.UniqueConstraint("org_id", "alias", name=op.f("uq_model_routes_org_id")),
    )
    with op.batch_alter_table("model_routes", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_model_routes_alias"), ["alias"], unique=False)
        batch_op.create_index(batch_op.f("ix_model_routes_org_id"), ["org_id"], unique=False)

    op.create_table(
        "project_tokens",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("org_id", sa.String(length=32), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], name=op.f("fk_project_tokens_org_id_organizations"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name=op.f("fk_project_tokens_project_id_projects"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_project_tokens")),
    )
    with op.batch_alter_table("project_tokens", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_project_tokens_org_id"), ["org_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_project_tokens_project_id"), ["project_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("project_tokens", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_project_tokens_project_id"))
        batch_op.drop_index(batch_op.f("ix_project_tokens_org_id"))

    op.drop_table("project_tokens")
    with op.batch_alter_table("model_routes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_model_routes_org_id"))
        batch_op.drop_index(batch_op.f("ix_model_routes_alias"))

    op.drop_table("model_routes")
    with op.batch_alter_table("dataset_uploads", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_dataset_uploads_dataset_id"))

    op.drop_table("dataset_uploads")
    with op.batch_alter_table("refresh_tokens", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_refresh_tokens_user_id"))

    op.drop_table("refresh_tokens")
    with op.batch_alter_table("providers", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_providers_org_id"))

    op.drop_table("providers")
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_projects_org_id"))

    op.drop_table("projects")
    with op.batch_alter_table("memberships", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_memberships_user_id"))
        batch_op.drop_index(batch_op.f("ix_memberships_org_id"))

    op.drop_table("memberships")
    with op.batch_alter_table("literature_docs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_literature_docs_org_id"))

    op.drop_table("literature_docs")
    with op.batch_alter_table("invitations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_invitations_org_id"))
        batch_op.drop_index(batch_op.f("ix_invitations_email"))

    op.drop_table("invitations")
    with op.batch_alter_table("datasets", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_datasets_org_id"))

    op.drop_table("datasets")
    with op.batch_alter_table("credit_ledger", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_credit_ledger_org_id"))

    op.drop_table("credit_ledger")
    with op.batch_alter_table("api_keys", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_api_keys_prefix"))
        batch_op.drop_index(batch_op.f("ix_api_keys_org_id"))

    op.drop_table("api_keys")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_users_email"))

    op.drop_table("users")
    with op.batch_alter_table("usage_events", schema=None) as batch_op:
        batch_op.drop_index("ix_usage_org_time")
        batch_op.drop_index(batch_op.f("ix_usage_events_project_id"))

    op.drop_table("usage_events")
    with op.batch_alter_table("organizations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_organizations_slug"))

    op.drop_table("organizations")
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.drop_index("ix_audit_org_time")

    op.drop_table("audit_logs")
