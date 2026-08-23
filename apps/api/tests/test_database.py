from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from hzcu_agent.config import Settings
from hzcu_agent.db import Database
from hzcu_agent.models import Conversation, new_id, utc_now


async def test_sqlite_enforces_integrity_and_applies_pilot_read_tuning(tmp_path) -> None:
    database_path = tmp_path / "foreign-key.db"
    database = Database(
        Settings(
            environment="test",
            database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        )
    )
    await database.initialize()
    try:
        async with database.engine.connect() as connection:
            assert (await connection.scalar(text("PRAGMA foreign_keys"))) == 1
            assert (await connection.scalar(text("PRAGMA busy_timeout"))) == 30_000
            assert (await connection.scalar(text("PRAGMA cache_size"))) == -65_536
            assert (await connection.scalar(text("PRAGMA temp_store"))) == 2
            assert (await connection.scalar(text("PRAGMA mmap_size"))) == 1_073_741_824

        async with database.session_factory() as session:
            now = utc_now()
            session.add(
                Conversation(
                    id=new_id("conv"),
                    owner_user_id="usr_missing",
                    profile_context={},
                    created_at=now,
                    updated_at=now,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
            else:
                raise AssertionError("SQLite accepted a dangling campus user reference")
    finally:
        await database.close()
