from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from .settings import settings


def make_db_url() -> str:
    if settings.DATABASE_URL:
        # SQLAlchemy async engine expects explicit asyncpg driver.
        return settings.DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1).replace(
            "postgresql://", "postgresql+asyncpg://", 1
        )
    return f"sqlite+aiosqlite:///{settings.SQLITE_PATH}"


DB_URL = make_db_url()
IS_POSTGRES = DB_URL.startswith("postgresql+asyncpg://")

engine = create_async_engine(DB_URL, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_backend_schema() -> None:
    """Create minimal schema required by API reads."""
    async with engine.begin() as conn:
        if IS_POSTGRES:
            await conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id BIGSERIAL PRIMARY KEY,
                        user_id BIGINT UNIQUE,
                        name TEXT NOT NULL UNIQUE
                    )
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS player_stats (
                        user_id BIGINT PRIMARY KEY,
                        total_points INTEGER DEFAULT 0,
                        matches_played INTEGER DEFAULT 0,
                        wins INTEGER DEFAULT 0
                    )
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS offseason_rating (
                        user_id BIGINT PRIMARY KEY,
                        slrpt INTEGER NOT NULL DEFAULT 0,
                        win_mult DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS verified_accounts (
                        user_id BIGINT PRIMARY KEY,
                        game_name TEXT NOT NULL,
                        game_uid TEXT NOT NULL UNIQUE,
                        verified_at TEXT NOT NULL,
                        operator_id BIGINT NOT NULL
                    )
                    """
                )
            )
            return

        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE,
                    name TEXT NOT NULL UNIQUE
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS player_stats (
                    user_id INTEGER PRIMARY KEY,
                    total_points INTEGER DEFAULT 0,
                    matches_played INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS offseason_rating (
                    user_id INTEGER PRIMARY KEY,
                    slrpt INTEGER NOT NULL DEFAULT 0,
                    win_mult REAL NOT NULL DEFAULT 1.0,
                    updated_at TEXT NOT NULL
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS verified_accounts (
                    user_id INTEGER PRIMARY KEY,
                    game_name TEXT NOT NULL,
                    game_uid TEXT NOT NULL UNIQUE,
                    verified_at TEXT NOT NULL,
                    operator_id INTEGER NOT NULL
                )
                """
            )
        )


async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
