"""Layout-preserving normalization for OCR engines that expose word boxes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hzcu_agent.ingestion.parsers import normalize_text


@dataclass(frozen=True)
class _PositionedLine:
    text: str
    left: float
    top: float
    width: float
    height: float

    @property
    def center_y(self) -> float:
        return self.top + self.height / 2


def normalized_ocr_text(payload: dict[str, Any]) -> str:
    """Return OCR text in visual row order when word coordinates are available."""

    raw_lines = payload.get("lines") or []
    positioned = [
        line for line_payload in raw_lines if (line := _positioned_line(line_payload)) is not None
    ]
    if positioned and len(positioned) == len(raw_lines):
        rows = _visual_rows(positioned)
        row_text = [
            " | ".join(line.text for line in sorted(row, key=lambda item: item.left))
            for row in rows
        ]
        # Two-column rows commonly carry the highest-value key/value or
        # date/event correspondence in forms and tables. Keep a compact copy
        # first, then retain the complete visual reading order.
        paired_rows = [text for row, text in zip(rows, row_text, strict=True) if len(row) == 2]
        text = "\n".join([*paired_rows, *row_text])
        return normalize_text(text)

    lines: list[str] = []
    for line in raw_lines:
        words = [
            normalize_text(str(value.get("text") if isinstance(value, dict) else value))
            for value in line.get("words") or []
        ]
        joined = "".join(value for value in words if value)
        if joined:
            lines.append(joined)
    if lines:
        return normalize_text("\n".join(lines))
    return normalize_text(str(payload.get("text") or ""))


def _positioned_line(payload: dict[str, Any]) -> _PositionedLine | None:
    words = []
    for value in payload.get("words") or []:
        if not isinstance(value, dict):
            return None
        text = normalize_text(str(value.get("text") or ""))
        box = value.get("box")
        if not text or not isinstance(box, dict):
            continue
        try:
            left = float(box["x"])
            top = float(box["y"])
            width = float(box["width"])
            height = float(box["height"])
        except (KeyError, TypeError, ValueError):
            return None
        words.append((text, left, top, width, height))
    if not words:
        return None
    words.sort(key=lambda item: item[1])
    left = min(item[1] for item in words)
    top = min(item[2] for item in words)
    right = max(item[1] + item[3] for item in words)
    bottom = max(item[2] + item[4] for item in words)
    return _PositionedLine(
        text="".join(item[0] for item in words),
        left=left,
        top=top,
        width=right - left,
        height=bottom - top,
    )


def _visual_rows(lines: list[_PositionedLine]) -> list[list[_PositionedLine]]:
    rows: list[list[_PositionedLine]] = []
    for line in sorted(lines, key=lambda item: (item.center_y, item.left)):
        best_row: list[_PositionedLine] | None = None
        best_distance: float | None = None
        for row in rows[-8:]:
            center = sum(item.center_y for item in row) / len(row)
            row_height = max(item.height for item in row)
            distance = abs(line.center_y - center)
            tolerance = max(4.0, min(line.height, row_height) * 0.65)
            if distance <= tolerance and (best_distance is None or distance < best_distance):
                best_row = row
                best_distance = distance
        if best_row is None:
            rows.append([line])
        else:
            best_row.append(line)
    rows.sort(key=lambda row: min(item.top for item in row))
    return rows
