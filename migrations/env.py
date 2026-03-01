import asyncio
import os
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config, create_async_engine
from sqlalchemy import pool

load_dotenv()

config = context.config

# Pull DATABASE_URL from env
DATABASE_URL: str = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set in .env")

# Sync URL for alembic offline mode (psycopg2)
DATABASE_URL_SYNC = DATABASE_URL.replace("postgresql+asyncpg", "postgresql+psycopg2")
config.set_main_option("sqlalchemy.url", DATABASE_URL_SYNC)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import models so alembic can detect them
from once.db.models import (  # noqa: E402, F401
    ConversationModel,
    ConversationParticipantModel,
    MediaModel,
    MessageMediaModel,
    MessageModel,
    UserModel,
)
from once.db.base import Base  # noqa: E402

target_metadata = Base.metadata


# ============================================================
# AUTO-CREATE DATABASE IF IT DOESN'T EXIST
# Connects to the default 'postgres' db, checks, then creates.
# ============================================================
def _extract_db_name(url: str) -> str:
    """Extract the database name from a postgres URL."""
    return url.rstrip("/").split("/")[-1].split("?")[0]


def _get_root_url(url: str) -> str:
    """Swap the target DB name for 'postgres' to get a root connection URL."""
    db_name = _extract_db_name(url)
    return url[: url.rfind(f"/{db_name}")] + "/postgres"


async def ensure_database_exists() -> None:
    db_name = _extract_db_name(DATABASE_URL)
    root_url = _get_root_url(DATABASE_URL)

    # Connect to postgres maintenance DB (isolation_level needed for CREATE DATABASE)
    engine = create_async_engine(root_url, isolation_level="AUTOCOMMIT", poolclass=pool.NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :db"), {"db": db_name}
            )
            exists = result.scalar() is not None
            if not exists:
                # Database names can't be parameterised in DDL — safe here as it comes from our own .env
                await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
                print(f"[alembic] Created database '{db_name}'")
            else:
                print(f"[alembic] Database '{db_name}' already exists.")
    finally:
        await engine.dispose()


# ============================================================
# MIGRATION RUNNERS
# ============================================================
def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Detect when DB schema is ahead of alembic history (e.g. manual changes)
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # Step 1: ensure DB exists before we try to connect to it
    await ensure_database_exists()

    # Step 2: run migrations against the actual target DB
    connectable = create_async_engine(DATABASE_URL, poolclass=pool.NullPool)
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()