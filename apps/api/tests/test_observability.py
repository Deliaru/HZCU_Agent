from __future__ import annotations

import json
import logging

from hzcu_agent.observability import JsonLogFormatter


def test_json_log_formatter_keeps_bounded_retrieval_diagnostics() -> None:
    record = logging.LogRecord(
        name="hzcu_agent.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="retrieval completed",
        args=(),
        exc_info=None,
    )
    record.query_variants = ["用户原表达", "官网式表达"]
    record.routed_sources = ["official-source"]
    record.candidate_ranking = [
        {"rank": 1, "canonical_url": "https://example.test/current"}
    ]
    record.deduplication = {"parent_assets_collapsed": 2}
    record.coverage_risk = False
    record.raw_output = "must-not-be-logged"

    payload = json.loads(JsonLogFormatter().format(record))

    assert payload["query_variants"] == ["用户原表达", "官网式表达"]
    assert payload["routed_sources"] == ["official-source"]
    assert payload["candidate_ranking"][0]["rank"] == 1
    assert payload["deduplication"] == {"parent_assets_collapsed": 2}
    assert payload["coverage_risk"] is False
    assert "raw_output" not in payload


def test_json_log_formatter_keeps_only_safe_structured_error_metadata() -> None:
    record = logging.LogRecord(
        name="hzcu_agent.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="structured repair failed",
        args=(),
        exc_info=None,
    )
    record.role = "compose"
    record.attempt = 2
    record.output_length = 127
    record.output_sha256 = "a" * 64
    record.error_types = ["missing"]
    record.error_paths = ["answer.headline"]
    record.invalid_output = "must-not-be-logged"

    payload = json.loads(JsonLogFormatter().format(record))

    assert payload["role"] == "compose"
    assert payload["attempt"] == 2
    assert payload["output_length"] == 127
    assert payload["output_sha256"] == "a" * 64
    assert payload["error_types"] == ["missing"]
    assert payload["error_paths"] == ["answer.headline"]
    assert "invalid_output" not in payload
