from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine


def make_engine(database_url: str) -> AsyncEngine:
    # hide_parameters: DB errors must never carry bound values (filenames, quotes) into logs.
    return create_async_engine(database_url, pool_pre_ping=True, hide_parameters=True)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One transaction per request: commit if the handler returns, roll back if it raises.

    Use with `Depends(get_session, scope="function")` so the commit happens before the
    response is sent. Repository functions only flush; they never commit.
    """
    async with request.app.state.sessionmaker.begin() as session:
        yield session
