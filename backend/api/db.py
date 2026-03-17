from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from .settings import settings

def make_db_url(sqlite_path: str) -> str:
    # aiosqlite нужен для async
    return f"sqlite+aiosqlite:///{sqlite_path}"

engine = create_async_engine(make_db_url(settings.SQLITE_PATH), echo=False, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def get_session() -> AsyncSession:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
