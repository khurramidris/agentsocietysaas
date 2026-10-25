"""Append-only audit log of security- and billing-relevant actions."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from .deps import client_ip
from .models import AuditLog


def audit(
    db: AsyncSession,
    request: Request | None,
    action: str,
    *,
    org_id: str | None = None,
    user_id: str | None = None,
    target_type: str = "",
    target_id: str = "",
    **details: Any,
) -> None:
    db.add(
        AuditLog(
            org_id=org_id,
            user_id=user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
            ip=client_ip(request) if request is not None else "",
        )
    )
