from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from hzcu_agent.config import Settings, get_settings


class Base(DeclarativeBase):
    pass


def _configure_sqlite_connection(
    dbapi_connection: object,
    _connection_record: object,
) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        # The pilot database is commonly hosted on a mounted Windows volume.
        # A large read-only observatory query otherwise pays an NTFS round
        # trip for every indexed chunk lookup.  Keep the cache bounded while
        # allowing SQLite to map the immutable/read-mostly pages directly.
        cursor.execute("PRAGMA cache_size=-65536")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.execute("PRAGMA mmap_size=1073741824")
    finally:
        cursor.close()


class Database:
    def __init__(self, settings: Settings) -> None:
        settings.ensure_local_data_directories()
        self._auto_create_schema = settings.environment != "production"
        connect_args = {"timeout": 30.0} if settings.database_url.startswith("sqlite") else {}
        self.engine: AsyncEngine = create_async_engine(
            settings.database_url,
            echo=False,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        if settings.database_url.startswith("sqlite"):
            event.listen(
                self.engine.sync_engine,
                "connect",
                _configure_sqlite_connection,
            )
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

    async def initialize(self) -> None:
        if not self._auto_create_schema:
            return
        from hzcu_agent import models  # noqa: F401

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()


_database: Database | None = None


def get_database() -> Database:
    global _database
    if _database is None:
        _database = Database(get_settings())
    return _database


async def get_session() -> AsyncIterator[AsyncSession]:
    database = get_database()
    async with database.session_factory() as session:
        yield session
