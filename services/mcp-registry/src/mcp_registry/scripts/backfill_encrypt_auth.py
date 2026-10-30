"""One-shot backfill: envelope-encrypt plaintext MCP server secrets at rest.

Phase 2 introduced envelope encryption for MCP auth secret fields
(``token`` / ``api_key`` / ``client_secret``); every ``env`` and ``headers``
value is encrypted too.  Rows written before that store plaintext; this script
rewrites them in place so the DB never holds plaintext after migration.

The script is idempotent:

  * ``encrypt_auth`` / ``encrypt_values`` skip values that already look like
    envelopes, so re-running on already-encrypted rows is a no-op.
  * Rows with no secret material (no auth secret, empty ``env`` / ``headers``)
    are skipped.

Operational notes
-----------------
  * Uses the migrator DSN (``MCP_REGISTRY_DB_DSN_MIGRATOR``) so it bypasses
    per-tenant RLS and can touch every tenant's rows in one pass.
  * The foundation crypto service must be reachable (``MCP_REGISTRY_CRYPTO_URL``).
    A row the crypto service can't encrypt is left untouched and counted as
    failed (the script then exits 1) - re-run once crypto is back.
  * Run inside the mcp-registry container::

      docker compose exec mcp-registry python -m mcp_registry.scripts.backfill_encrypt_auth

  * Dev caveat (shared with llm-gateway): the Vault transit backend is
    ephemeral; after a vault reset, existing envelopes become undecryptable.
    Re-create affected MCP servers or re-seed rather than re-running this
    script (re-encrypting plaintext that is already an envelope is a no-op,
    so a reset cannot be recovered by re-running).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from mcp_registry.config import Settings
from mcp_registry.domain.models import McpServer
from mcp_registry.errors import SecretEncryptionUnavailableError
from mcp_registry.services.secret_crypto import encrypt_auth, encrypt_values

log = logging.getLogger("backfill_encrypt_auth")


async def _encrypt_row(
    session: AsyncSession,
    server: McpServer,
    *,
    crypto_url: str,
    crypto_timeout: float,
) -> bool:
    """Encrypt plaintext secrets on ``server`` in place. Return True if changed.

    Raises ``SecretEncryptionUnavailableError`` (row untouched) when crypto
    can't encrypt a value - this script never stores plaintext as a fallback.
    """
    ctx: dict[str, Any] = {
        "crypto_url": crypto_url, "timeout": crypto_timeout, "tenant_id": server.tenant_id,
    }
    # Both helpers are idempotent (skip envelopes); ``encrypt_auth`` only
    # touches the secret fields for the configured auth type.
    auth = await encrypt_auth(server.auth, **ctx) if isinstance(server.auth, dict) else server.auth
    env = await encrypt_values(server.env, **ctx)
    headers = await encrypt_values(server.headers, **ctx)
    if (auth, env, headers) == (server.auth, server.env or {}, server.headers or {}):
        return False
    server.auth, server.env, server.headers = auth, env, headers
    return True


async def run(*, dry_run: bool, batch_size: int) -> int:
    settings = Settings()
    log.info(
        "backfill_start db=%s crypto=%s dry_run=%s",
        settings.db_dsn_migrator.split("@")[-1],  # don't log credentials
        settings.crypto_url,
        dry_run,
    )
    engine = create_async_engine(settings.db_dsn_migrator)
    encrypted_count = 0
    skipped_count = 0
    failed_count = 0
    try:
        async with engine.begin() as conn:
            # Migrator role owns the schema; no RLS enforcement.  Still set
            # search_path so the ORM finds the table on Postgres.
            await conn.execute(text(f'SET search_path TO "{settings.db_schema}", public'))
        async with AsyncSession(engine, expire_on_commit=False) as session:
            result = await session.execute(select(McpServer))
            servers = list(result.scalars().all())
            log.info("backfill_candidates total=%d", len(servers))
            for server in servers:
                try:
                    changed = await _encrypt_row(
                        session,
                        server,
                        crypto_url=settings.crypto_url,
                        crypto_timeout=settings.crypto_timeout,
                    )
                except SecretEncryptionUnavailableError:
                    failed_count += 1
                    log.warning(
                        "backfill_failed id=%s tenant=%s (crypto unavailable)",
                        server.id, server.tenant_id,
                    )
                    continue
                if changed:
                    encrypted_count += 1
                    log.info(
                        "backfill_encrypted id=%s tenant=%s name=%s",
                        server.id, server.tenant_id, server.name,
                    )
                    if encrypted_count % batch_size == 0:
                        await session.flush()
                else:
                    skipped_count += 1
            if dry_run:
                log.info("backfill_dry_run rolling back (no writes)")
                await session.rollback()
            else:
                await session.commit()
    finally:
        await engine.dispose()

    log.info(
        "backfill_done encrypted=%d skipped=%d failed=%d dry_run=%s",
        encrypted_count, skipped_count, failed_count, dry_run,
    )
    return 1 if failed_count else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the encrypted values but roll back - no DB writes.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Flush every N rows (default 50).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return asyncio.run(run(dry_run=args.dry_run, batch_size=args.batch_size))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
