"""Report Stage 5 grounding and controllable-performance metrics from the database."""

from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import select

from hzcu_agent.config import get_settings
from hzcu_agent.db import Database
from hzcu_agent.models import AnswerGroundingRecord, TaskPerformanceRecord
from hzcu_agent.services.stage5_metrics import build_stage5_metrics


async def _run(database_url: str | None, minimum_samples: int) -> dict:
    settings = get_settings()
    if database_url:
        settings = settings.model_copy(update={"database_url": database_url})
    database = Database(settings)
    try:
        async with database.session_factory() as session:
            performance = list(
                (await session.scalars(select(TaskPerformanceRecord))).all()
            )
            grounding = list(
                (await session.scalars(select(AnswerGroundingRecord))).all()
            )
        return build_stage5_metrics(
            performance,
            grounding,
            minimum_samples_per_scenario=minimum_samples,
        )
    finally:
        await database.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url")
    parser.add_argument("--minimum-samples", type=int, default=20)
    args = parser.parse_args()
    if args.minimum_samples < 1:
        parser.error("--minimum-samples 必须大于 0")
    report = asyncio.run(_run(args.database_url, args.minimum_samples))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
