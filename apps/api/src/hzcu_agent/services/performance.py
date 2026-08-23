from __future__ import annotations

import time
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Literal

from hzcu_agent.schemas import AgentPerformance

SpanKind = Literal["model", "tool", "local"]


@dataclass
class PerformanceSpan:
    kind: SpanKind
    name: str
    started_ns: int
    first_event_ns: int | None = None
    completed_ns: int | None = None
    ttft_measurable: bool = True


class AgentPerformanceTrace:
    """Per-task critical-path timing without recording prompts or tool payloads."""

    def __init__(self) -> None:
        self.started_ns = time.perf_counter_ns()
        self.completed_ns: int | None = None
        self.first_progress_ns: int | None = None
        self.spans: list[PerformanceSpan] = []
        self._scenario_hints: set[str] = set()

    def start_span(self, kind: SpanKind, name: str) -> PerformanceSpan:
        span = PerformanceSpan(kind=kind, name=name, started_ns=time.perf_counter_ns())
        self.spans.append(span)
        return span

    @staticmethod
    def mark_first_event(span: PerformanceSpan) -> None:
        if span.first_event_ns is None:
            span.first_event_ns = time.perf_counter_ns()

    @staticmethod
    def mark_unmeasurable(span: PerformanceSpan) -> None:
        span.ttft_measurable = False

    @staticmethod
    def finish_span(span: PerformanceSpan) -> None:
        if span.completed_ns is None:
            span.completed_ns = time.perf_counter_ns()

    def mark_progress(self) -> None:
        if self.first_progress_ns is None:
            self.first_progress_ns = time.perf_counter_ns()

    def add_scenario_hint(self, value: str) -> None:
        self._scenario_hints.add(value)

    def finish(self) -> None:
        if self.completed_ns is None:
            self.completed_ns = time.perf_counter_ns()

    def snapshot(self) -> tuple[AgentPerformance, list[dict[str, object]]]:
        self.finish()
        assert self.completed_ns is not None
        ttft_intervals = [
            (span.started_ns, span.first_event_ns)
            for span in self.spans
            if span.kind == "model" and span.ttft_measurable and span.first_event_ns is not None
        ]
        excluded_ns = _interval_union_duration(ttft_intervals)
        total_ns = max(0, self.completed_ns - self.started_ns)
        first_progress_ms = (
            _milliseconds(self.first_progress_ns - self.started_ns)
            if self.first_progress_ns is not None
            else None
        )
        model_spans = [span for span in self.spans if span.kind == "model"]
        tool_spans = [span for span in self.spans if span.kind == "tool"]
        measurable = all(
            span.ttft_measurable and span.first_event_ns is not None for span in model_spans
        )
        spans = [
            {
                "kind": span.kind,
                "name": span.name,
                "started_ms": _milliseconds(span.started_ns - self.started_ns),
                "first_event_ms": (
                    _milliseconds(span.first_event_ns - self.started_ns)
                    if span.first_event_ns is not None
                    else None
                ),
                "completed_ms": (
                    _milliseconds(span.completed_ns - self.started_ns)
                    if span.completed_ns is not None
                    else None
                ),
                "duration_ms": (
                    _milliseconds(span.completed_ns - span.started_ns)
                    if span.completed_ns is not None
                    else None
                ),
                "ttft_measurable": span.ttft_measurable,
            }
            for span in self.spans
        ]
        performance = AgentPerformance(
            scenario=self._scenario(),
            total_duration_ms=_milliseconds(total_ns),
            excluded_model_ttft_ms=_milliseconds(excluded_ns),
            controllable_duration_ms=_milliseconds(max(0, total_ns - excluded_ns)),
            first_progress_ms=first_progress_ms,
            model_call_count=len(model_spans),
            tool_call_count=len(tool_spans),
            model_ttft_measurable=measurable,
            spans=spans,
        )
        return performance, spans

    def _scenario(self) -> str:
        if "image" in self._scenario_hints or "multi_source" in self._scenario_hints:
            return "multi_source_or_image"
        if "campus_authenticated" in self._scenario_hints:
            return "campus_authenticated"
        if "public_live" in self._scenario_hints:
            return "public_live"
        return "no_live_read"


_current_trace: ContextVar[AgentPerformanceTrace | None] = ContextVar(
    "agent_performance_trace",
    default=None,
)


def bind_performance_trace(
    trace: AgentPerformanceTrace,
) -> Token[AgentPerformanceTrace | None]:
    return _current_trace.set(trace)


def reset_performance_trace(token: Token[AgentPerformanceTrace | None]) -> None:
    _current_trace.reset(token)


def current_performance_trace() -> AgentPerformanceTrace | None:
    return _current_trace.get()


def _interval_union_duration(intervals: list[tuple[int, int]]) -> int:
    if not intervals:
        return 0
    ordered = sorted(intervals)
    total = 0
    start, end = ordered[0]
    for next_start, next_end in ordered[1:]:
        if next_start <= end:
            end = max(end, next_end)
            continue
        total += max(0, end - start)
        start, end = next_start, next_end
    total += max(0, end - start)
    return total


def _milliseconds(nanoseconds: int) -> float:
    return round(nanoseconds / 1_000_000, 2)
