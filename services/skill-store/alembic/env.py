"""Alembic env.py for skill-store."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from skill_store.config import Settings
from skill_store.domain.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = Settings()
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.db_dsn_migrator,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    # Foundation isolates every service in its own schema (app_migrator owns
    # it; public is not writable).  Create it up-front, route unqualified DDL
    # into it via search_path, and keep alembic_version in the same schema.
    schema = settings.db_schema
    connection.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    # Keep ``public`` on the path for extension-provided types; service DDL
    # still defaults into the service schema (listed first).
    connection.exec_driver_sql(f'SET search_path TO "{schema}", public')
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=schema,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(settings.db_dsn_migrator)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
        # ``connect()`` (vs ``begin()``) does not auto-commit; commit the
        # schema bootstrap + alembic DDL explicitly or asyncpg rolls them
        # back on connection close.
        await connection.commit()
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
