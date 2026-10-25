"""Model catalog seeding.

Prices are customer prices in nano-USD per token (``1 USD per MTok == 1000``).
Anthropic list prices below are the first-party Claude API rates as of 2026-10
(cache reads per model, cache writes 1.25x input for the 5-minute TTL and 2x for
the 1-hour TTL); re-check https://www.anthropic.com/pricing before launch.
Other providers are added from an operator-maintained catalog file
(``CP_MODEL_CATALOG_FILE``, see deploy/model_catalog.example.json) because their
prices change often and must not be guessed.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ModelRoute, Provider
from ..security import Encryptor
from ..settings import Settings

log = logging.getLogger("controlplane.gateway")


def usd_per_mtok(value: float) -> int:
    return int(round(value * 1000))


# alias, description, input, output, cache read, cache write 5m, cache write 1h, long ctx tokens, multiplier
ANTHROPIC_MODELS = [
    ("claude-opus-5-5", "Claude Opus 5.5", 4.00, 20.00, 0.20, 5.00, 8.00, 0, 1.0),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5", 2.00, 10.00, 0.20, 2.50, 4.00, 0, 1.0),
    # $0.10/$0.50 up to 100K prompt tokens, $0.50/$2.50 beyond.
    ("claude-haiku-5-5", "Claude Haiku 5.5", 0.10, 0.50, 0.01, 0.125, 0.20, 100_000, 5.0),
    ("claude-fable-5-1", "Claude Fable 5.1", 10.00, 50.00, 0.25, 12.50, 20.00, 0, 1.0),
]

MOCK_MODELS = ["mock", "mock-embedding"]


async def _ensure_provider(db: AsyncSession, *, name: str, kind: str, api_base: str, api_key_enc: str | None) -> Provider:
    existing = (
        await db.execute(select(Provider).where(Provider.org_id.is_(None), Provider.name == name))
    ).scalar_one_or_none()
    if existing is not None:
        if api_key_enc and not existing.api_key_enc:
            existing.api_key_enc = api_key_enc
        return existing
    provider = Provider(name=name, kind=kind, api_base=api_base, api_key_enc=api_key_enc)
    db.add(provider)
    await db.flush()
    return provider


async def _ensure_route(db: AsyncSession, provider: Provider, alias: str, **fields) -> None:
    existing = (
        await db.execute(select(ModelRoute).where(ModelRoute.org_id.is_(None), ModelRoute.alias == alias))
    ).scalar_one_or_none()
    if existing is None:
        db.add(ModelRoute(alias=alias, provider_id=provider.id, **fields))


async def seed_catalog(db: AsyncSession, settings: Settings, encryptor: Encryptor) -> None:
    mult = settings.price_multiplier
    if settings.anthropic_api_key:
        provider = await _ensure_provider(
            db, name="anthropic", kind="anthropic", api_base=settings.anthropic_api_base,
            api_key_enc=encryptor.encrypt(settings.anthropic_api_key),
        )
        for alias, desc, inp, out, cr, cw5, cw1h, lct, lcm in ANTHROPIC_MODELS:
            await _ensure_route(
                db, provider, alias,
                upstream_model=alias, kind="chat", description=desc,
                input_nanos=usd_per_mtok(inp * mult), output_nanos=usd_per_mtok(out * mult),
                cache_read_nanos=usd_per_mtok(cr * mult), cache_write_nanos=usd_per_mtok(cw5 * mult),
                cache_write_1h_nanos=usd_per_mtok(cw1h * mult),
                long_context_tokens=lct, long_context_multiplier=lcm,
            )
    if settings.openai_api_key:
        await _ensure_provider(
            db, name="openai", kind="openai", api_base=settings.openai_api_base,
            api_key_enc=encryptor.encrypt(settings.openai_api_key),
        )
    if settings.enable_mock_provider:
        mock = await _ensure_provider(db, name="mock", kind="mock", api_base="", api_key_enc=None)
        await _ensure_route(db, mock, "mock", upstream_model="mock", kind="chat", description="Deterministic mock chat model")
        await _ensure_route(db, mock, "mock-embedding", upstream_model="mock-embedding", kind="embedding", description="Deterministic mock embeddings")
    if settings.model_catalog_file:
        await load_catalog_file(db, Path(settings.model_catalog_file), encryptor, mult)
    await db.commit()


async def load_catalog_file(db: AsyncSession, path: Path, encryptor: Encryptor, mult: float) -> None:
    """Load providers/routes from JSON: {"providers": [...], "models": [...]}."""
    data = json.loads(path.read_text(encoding="utf-8"))
    providers: dict[str, Provider] = {}
    for item in data.get("providers", []):
        key_env = item.get("api_key_env")
        key = os.environ.get(key_env, "") if key_env else ""
        providers[item["name"]] = await _ensure_provider(
            db, name=item["name"], kind=item["kind"], api_base=item.get("api_base", ""),
            api_key_enc=encryptor.encrypt(key) if key else None,
        )
    for model in data.get("models", []):
        provider = providers.get(model["provider"])
        if provider is None:
            log.warning("catalog model %s references unknown provider %s", model.get("alias"), model.get("provider"))
            continue
        await _ensure_route(
            db, provider, model["alias"],
            upstream_model=model.get("upstream_model", model["alias"]),
            kind=model.get("kind", "chat"),
            description=model.get("description", ""),
            input_nanos=usd_per_mtok(model.get("input_usd_per_mtok", 0) * mult),
            output_nanos=usd_per_mtok(model.get("output_usd_per_mtok", 0) * mult),
            cache_read_nanos=usd_per_mtok(model.get("cache_read_usd_per_mtok", 0) * mult),
            cache_write_nanos=usd_per_mtok(model.get("cache_write_usd_per_mtok", 0) * mult),
            cache_write_1h_nanos=usd_per_mtok(model.get("cache_write_1h_usd_per_mtok", 0) * mult),
            enabled=bool(model.get("enabled", True)),
        )
