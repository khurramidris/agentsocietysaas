"""Database migrations (Alembic), usable without an alembic.ini file.

``python -m controlplane migrate`` upgrades to the latest revision. The API also
does this on start unless ``CP_AUTO_MIGRATE=false`` (recommended when several
replicas start at once: run the migration as a release step instead).
"""

from __future__ import annotations

from alembic import command
from alembic.config import Config


def alembic_config(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", "controlplane:migrations")
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


def upgrade(database_url: str, revision: str = "head") -> None:
    """Blocking; run it in a worker thread from async code."""
    command.upgrade(alembic_config(database_url), revision)


def autogenerate(database_url: str, message: str) -> None:
    """Developer helper: write a new revision from the difference between models and DB."""
    command.revision(alembic_config(database_url), message=message, autogenerate=True)
