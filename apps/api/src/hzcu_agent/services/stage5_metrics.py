from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from hzcu_agent.models import AnswerGroundingRecord, TaskPerformanceRecord

SCENARIO_LIMITS_MS = {
    "no_live_read": 5_000.0,
    "public_live": 15_000.0,
    "campus_authenticated": 35_000.0,
    "multi_source_or_image": 60_000.0,
}


def nearest_rank(values: Iterable[float | int], percentile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    rank = max(1, math.ceil(percentile * len(ordered)))
    return round(ordered[rank - 1], 2)


def build_stage5_metrics(
    performance_records: Iterable[TaskPerformanceRecord],
    grounding_records: Iterable[AnswerGroundingRecord],
    *,
    minimum_samples_per_scenario: int = 20,
) -> dict[str, Any]:
    performance = list(performance_records)
    grounding = list(grounding_records)
    performance_by_scenario: dict[str, list[TaskPerformanceRecord]] = defaultdict(list)
    for record in performance:
        performance_by_scenario[record.scenario].append(record)

    scenario_metrics: dict[str, dict[str, Any]] = {}
    for scenario, limit_ms in SCENARIO_LIMITS_MS.items():
        records = performance_by_scenario.get(scenario, [])
        measurable = [record for record in records if record.model_ttft_measurable]
        p95 = nearest_rank(
            (record.controllable_duration_ms for record in measurable),
            0.95,
        )
        enough_samples = len(measurable) >= minimum_samples_per_scenario
        scenario_metrics[scenario] = {
            "sample_count": len(records),
            "measurable_sample_count": len(measurable),
            "excluded_unmeasurable_count": len(records) - len(measurable),
            "controllable_duration_ms": {
                "p50": nearest_rank(
                    (record.controllable_duration_ms for record in measurable),
                    0.50,
                ),
                "p95": p95,
                "p99": nearest_rank(
                    (record.controllable_duration_ms for record in measurable),
                    0.99,
                ),
                "limit_p95": limit_ms,
            },
            "model_call_count_p95": nearest_rank(
                (record.model_call_count for record in measurable),
                0.95,
            ),
            "first_progress_ms_p95": nearest_rank(
                (
                    record.first_progress_ms
                    for record in measurable
                    if record.first_progress_ms is not None
                ),
                0.95,
            ),
            "status": (
                "insufficient_samples"
                if not enough_samples
                else "passed"
                if p95 is not None and p95 <= limit_ms
                else "failed"
            ),
        }

    finding_counts: Counter[str] = Counter()
    error_finding_count = 0
    for record in grounding:
        for finding in record.findings:
            code = str(finding.get("code") or "unknown")
            finding_counts[code] += 1
            if finding.get("severity") == "error":
                error_finding_count += 1

    coverage_values = [record.citation_coverage for record in grounding]
    support_values = [record.fully_supported_rate for record in grounding]
    quality = {
        "answer_sample_count": len(grounding),
        "citation_coverage_mean": _mean(coverage_values),
        "citation_coverage_min": min(coverage_values, default=None),
        "fully_supported_rate_mean": _mean(support_values),
        "fully_supported_rate_min": min(support_values, default=None),
        "answers_with_complete_citation_coverage": sum(value == 1.0 for value in coverage_values),
        "structural_error_finding_count": error_finding_count,
        "finding_counts": dict(sorted(finding_counts.items())),
        "citation_support_accuracy": {
            "status": "requires_labeled_evaluation",
            "note": "结构化自检不能替代人工或独立标注的引用支持准确率。",
        },
    }

    perf_statuses = {item["status"] for item in scenario_metrics.values()}
    performance_gate = (
        "failed"
        if "failed" in perf_statuses
        else "insufficient_samples"
        if "insufficient_samples" in perf_statuses
        else "passed"
    )
    protocol_quality_passed = (
        bool(grounding)
        and all(value == 1.0 for value in coverage_values)
        and error_finding_count == 0
    )
    return {
        "quality": quality,
        "performance": {
            "minimum_samples_per_scenario": minimum_samples_per_scenario,
            "scenarios": scenario_metrics,
            "gate_status": performance_gate,
            "model_ttft_excluded_from_gate": True,
        },
        "stage5_gate_status": (
            "incomplete"
            if performance_gate == "insufficient_samples"
            or quality["citation_support_accuracy"]["status"] != "measured"
            else "failed"
            if performance_gate == "failed" or not protocol_quality_passed
            else "passed"
        ),
    }


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)
