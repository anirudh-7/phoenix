"""Async database engine and session factory.

One engine per process. Sessions are short-lived, one per request or per unit of work.

Usage in FastAPI:

    @app.get("/runs")
    async def list_runs(session: AsyncSession = Depends(get_session)) -> list[Run]:
        ...

Usage in background workers:

    async with session_factory() as session:
        async with session.begin():
            ...
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from phoenix.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,                         # set True for SQL logging during dev
    pool_pre_ping=True,                 # transparent reconnect after DB restart
    pool_size=5,
    max_overflow=5,
)

session_factory = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,             # let objects stay usable after commit
    class_=AsyncSession,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped async session."""
    async with session_factory() as session:
        yield session
