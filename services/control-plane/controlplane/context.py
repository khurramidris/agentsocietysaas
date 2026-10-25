"""Process-wide services shared by all requests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .billing import Stripe
from .db import Database
from .security import Encryptor
from .settings import Settings
from .storage import BlobStore, make_blob_store

if TYPE_CHECKING:
    from .gateway.metering import UsageRecorder
    from .gateway.router import Gateway
    from .literature.server import LiteratureService
    from .sandboxes.manager import SandboxManager


@dataclass
class AppContext:
    settings: Settings
    db: Database
    encryptor: Encryptor
    blobs: BlobStore
    stripe: Stripe
    usage: UsageRecorder = field(init=False)
    gateway: Gateway = field(init=False)
    sandboxes: SandboxManager = field(init=False)
    literature: LiteratureService = field(init=False)

    @classmethod
    def create(cls, settings: Settings, *, sandbox_driver=None) -> AppContext:
        from .gateway.metering import UsageRecorder
        from .gateway.router import Gateway
        from .literature.server import LiteratureService
        from .sandboxes.manager import SandboxManager

        ctx = cls(
            settings=settings,
            db=Database(settings.database_url),
            encryptor=Encryptor(settings),
            blobs=make_blob_store(settings),
            stripe=Stripe(settings),
        )
        ctx.usage = UsageRecorder(ctx)
        ctx.gateway = Gateway(ctx)
        ctx.sandboxes = SandboxManager(ctx, driver=sandbox_driver)
        ctx.literature = LiteratureService(ctx)
        return ctx

    async def close(self) -> None:
        await self.sandboxes.close()
        await self.usage.close()
        await self.gateway.close()
        await self.literature.close()
        await self.stripe.client.aclose()
        await self.db.dispose()
