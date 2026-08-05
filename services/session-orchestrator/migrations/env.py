"""Alembic env.py for session-orchestrator."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from agent_session_orchestrator.config import Settings
from agent_session_orchestrator.domain.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = Settings()
target_metadata = Base.metadata

# Foundation isolates this service in its own schema (app_migrator owns it;
# public is not writable).  Hardcoded rather than read from settings because
# the service has no ``db_schema`` config - every migration qualifies DDL with
# this name.  Mirrors the peer-service env.py pattern (app-registry,
# cron-runner, ...).
SCHEMA = "session_orchestrator"


def run_migrations_offline() -> None:
    context.configure(
        url=settings.db_dsn_migrator,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    # Create the service schema up-front so alembic can place its
    # ``alembic_version`` table there before 0001 (which also CREATE SCHEMA)
    # runs.  Without this, the first ``alembic upgrade head`` on a fresh DB
    # fails: alembic creates the version table in ``session_orchestrator``
    # before any migration executes, but the schema only comes into existence
    # inside 0001 - a chicken-and-egg that stays hidden on dev DBs that
    # already have the schema.
    connection.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
    # Keep ``public`` on the path for extension-provided types; service DDL
    # still defaults into the service schema (listed first).
    connection.exec_driver_sql(f'SET search_path TO "{SCHEMA}", public')
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=SCHEMA,
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
