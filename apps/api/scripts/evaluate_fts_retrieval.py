from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections import defaultdict
from pathlib import Path

from hzcu_agent.config import Settings
from hzcu_agent.db import Database
from hzcu_agent.tools.campus_memory import (
    CampusMemorySearchArguments,
    CampusMemorySearchTool,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate the frozen campus FTS corpus.")
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path(__file__).parents[1]
        / "tests"
        / "fixtures"
        / "campus_fts_eval_v1.json",
    )
    parser.add_argument("--database-url")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--output", type=Path)
    return parser


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile)))
    return ordered[index]


async def _run(args: argparse.Namespace) -> int:
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    settings_kwargs = {}
    if args.database_url:
        settings_kwargs["database_url"] = args.database_url
    database = Database(Settings(**settings_kwargs))
    memory = CampusMemorySearchTool(database)
    cold_started = time.perf_counter()
    await memory.initialize()
    cold_index_ms = (time.perf_counter() - cold_started) * 1000

    cases: list[dict] = []
    durations_ms: list[float] = []
    domain_totals: dict[str, list[bool]] = defaultdict(list)
    formal_top3: list[bool] = []
    expected_url_count = 0
    matched_url_count = 0
    try:
        for case in fixture["cases"]:
            ranked_urls: list[str] = []
            query_results: list[dict] = []
            for query in case["retrieval_queries"]:
                started = time.perf_counter()
                result = await memory.run(
                    CampusMemorySearchArguments(query=query, top_k=args.top_k),
                    trace_id=f"eval-{case['id']}",
                    allowed_visibilities=frozenset({"public", "campus"}),
                )
                elapsed_ms = (time.perf_counter() - started) * 1000
                durations_ms.append(elapsed_ms)
                urls = [item.canonical_url for item in result.evidence]
                for url in urls:
                    if url not in ranked_urls:
                        ranked_urls.append(url)
                query_results.append(
                    {
                        "query": query,
                        "duration_ms": round(elapsed_ms, 3),
                        "urls": urls,
                        "error": (
                            result.error.model_dump(mode="json") if result.error else None
                        ),
                    }
                )
            missing = [
                url for url in case["expected_urls"] if url not in ranked_urls
            ]
            expected_url_count += len(case["expected_urls"])
            matched_url_count += len(case["expected_urls"]) - len(missing)
            passed = not missing
            domain_totals[case["domain"]].append(passed)
            if (
                case["shape"] == "formal"
                and len(case["retrieval_queries"]) == 1
                and len(case["expected_urls"]) == 1
            ):
                formal_top3.append(case["expected_urls"][0] in ranked_urls[:3])
            cases.append(
                {
                    "id": case["id"],
                    "domain": case["domain"],
                    "shape": case["shape"],
                    "passed": passed,
                    "missing_urls": missing,
                    "queries": query_results,
                }
            )
    finally:
        await database.close()

    passed_count = sum(case["passed"] for case in cases)
    summary = {
        "fixture_version": fixture["version"],
        "case_count": len(cases),
        "passed_count": passed_count,
        "top8_case_recall": round(passed_count / len(cases), 4),
        "top8_expected_url_recall": round(
            matched_url_count / expected_url_count,
            4,
        ),
        "formal_title_top3_recall": (
            round(sum(formal_top3) / len(formal_top3), 4) if formal_top3 else None
        ),
        "cold_index_initialization_ms": round(cold_index_ms, 3),
        "query_latency_ms": {
            "count": len(durations_ms),
            "mean": round(statistics.fmean(durations_ms), 3),
            "p50": round(_percentile(durations_ms, 0.50), 3),
            "p95": round(_percentile(durations_ms, 0.95), 3),
            "max": round(max(durations_ms), 3),
        },
        "domains": {
            domain: {
                "passed": sum(values),
                "count": len(values),
                "recall": round(sum(values) / len(values), 4),
            }
            for domain, values in sorted(domain_totals.items())
        },
    }
    report = {"summary": summary, "cases": cases}
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    print(output)
    return 0 if summary["top8_expected_url_recall"] >= 0.95 else 1


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parser().parse_args())))


if __name__ == "__main__":
    main()
