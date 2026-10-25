"""FastAPI application factory for the control plane."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__, migrate
from .api import admin, auth, billing, catalog, datasets, literature, orgs, projects, proxy
from .context import AppContext
from .gateway import router as gateway_router
from .gateway.catalog import seed_catalog
from .literature.server import AuthenticatedMCP
from .models import User
from .settings import Settings, get_settings

log = logging.getLogger("controlplane")


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if not request.url.path.startswith("/ide/"):
            response.headers.setdefault("X-Frame-Options", "DENY")
        return response


async def _bootstrap_admin(ctx: AppContext) -> None:
    s = ctx.settings
    if not (s.bootstrap_admin_email and s.bootstrap_admin_password):
        return
    async with ctx.db.sessionmaker() as db:
        email = s.bootstrap_admin_email.lower()
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            user = await auth.create_user_with_org(db, s, email=email, password=s.bootstrap_admin_password, name="Administrator", org_name=None, admin=True)
            log.info("created bootstrap platform admin %s", email)
        elif not user.is_platform_admin:
            user.is_platform_admin = True
        await db.commit()


def create_app(settings: Settings | None = None, *, sandbox_driver=None) -> FastAPI:
    settings = settings or get_settings()
    if settings.is_production:
        problems = settings.validate_for_production()
        if problems:
            raise RuntimeError("Refusing to start in production:\n- " + "\n- ".join(problems))
    ctx = AppContext.create(settings, sandbox_driver=sandbox_driver)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
        if settings.auto_migrate:
            await asyncio.to_thread(migrate.upgrade, settings.database_url)
        async with ctx.db.sessionmaker() as db:
            await seed_catalog(db, settings, ctx.encryptor)
        await _bootstrap_admin(ctx)
        ctx.usage.start()
        ctx.sandboxes.start_reaper()
        async with ctx.literature.server.session_manager.run():
            try:
                yield
            finally:
                await ctx.close()

    app = FastAPI(
        title=f"{settings.brand_name} API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.ctx = ctx
    app.add_middleware(SecurityHeaders)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    for module in (auth, orgs, projects, proxy, billing, admin, datasets, literature):
        app.include_router(module.router)
    app.include_router(admin.meta_router)
    app.include_router(catalog.router)
    app.include_router(catalog.admin)
    app.include_router(gateway_router.router)
    app.mount("/literature", AuthenticatedMCP(ctx, ctx.literature))

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok", "version": __version__}

    _mount_spa(app, settings)
    return app


def _mount_spa(app: FastAPI, settings: Settings) -> None:
    """Serve the built web app (services/web/dist) with client-side routing fallback."""
    dist = Path(settings.web_dist_dir).resolve() if settings.web_dist_dir else None
    if dist is None or not (dist / "index.html").exists():
        return
    reserved = ("/api/", "/llm/", "/literature/", "/ide/", "/healthz")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str, request: Request):
        if request.url.path.startswith(reserved):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        candidate = (dist / path).resolve()
        if path and dist in candidate.parents and candidate.is_file():
            headers = {"Cache-Control": "public, max-age=31536000, immutable"} if path.startswith("assets/") else {}
            return FileResponse(candidate, headers=headers)
        return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})
