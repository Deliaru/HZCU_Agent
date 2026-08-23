"""Transcribe rendered document pages with the configured multimodal model.

Rendering and OCR are deliberately separate, atomic operations. This command
accepts ordered page images, writes one resumable UTF-8 text file per image,
then assembles a page-addressable Markdown document for generic exploration.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
from pathlib import Path

from accept_stage4 import _load_api_config

from hzcu_agent.auth.campus_access import CampusNoticeImage
from hzcu_agent.config import Settings
from hzcu_agent.services.image_reader import CampusImageReader

DEFAULT_MODEL = "gpt-5.6-terra"
SUPPORTED_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
NATURAL_NUMBER = re.compile(r"(\d+)")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Faithfully OCR ordered document page images with the configured model."
    )
    parser.add_argument("page_directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--work-directory",
        type=Path,
        help="Resumable per-page text directory; defaults beside the output.",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument(
        "--batch-size",
        type=int,
        choices=range(1, 7),
        default=1,
        metavar="1..6",
        help="Pages per model call. Dense tables usually work best with 1 or 2.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Transcribe pages again even when a non-empty resumable result exists.",
    )
    return parser.parse_args()


def _natural_key(path: Path) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold() for part in NATURAL_NUMBER.split(path.name)
    )


def _page_images(page_directory: Path) -> list[Path]:
    pages = sorted(
        (
            path
            for path in page_directory.iterdir()
            if path.is_file() and path.suffix.casefold() in SUPPORTED_IMAGE_SUFFIXES
        ),
        key=_natural_key,
    )
    if not pages:
        raise RuntimeError(f"No supported page images found in {page_directory}")
    return pages


def _data_url(path: Path) -> str:
    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }[path.suffix.casefold()]
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


async def _run(args: argparse.Namespace) -> dict[str, object]:
    pages = _page_images(args.page_directory.resolve())
    output = args.output.resolve()
    work_directory = (
        args.work_directory.resolve()
        if args.work_directory
        else output.parent / f".{output.stem}-pages"
    )
    work_directory.mkdir(parents=True, exist_ok=True)

    base_url, api_key = _load_api_config()
    settings = Settings(
        model_provider="openai",
        openai_api_key=api_key,
        openai_base_url=base_url,
        utility_model=args.model,
        model_timeout_seconds=args.timeout_seconds,
    )
    reader = CampusImageReader(settings)
    completed = 0
    reused = 0
    try:
        for batch_start in range(0, len(pages), args.batch_size):
            batch = list(
                enumerate(
                    pages[batch_start : batch_start + args.batch_size],
                    start=batch_start + 1,
                )
            )
            pending: list[tuple[int, Path, Path]] = []
            for page_number, page_path in batch:
                page_result = work_directory / f"page-{page_number:04d}.txt"
                if not args.force and page_result.is_file():
                    existing = page_result.read_text(encoding="utf-8").strip()
                    if existing:
                        reused += 1
                        print(
                            json.dumps(
                                {
                                    "page": page_number,
                                    "pages": len(pages),
                                    "status": "reused",
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        continue
                pending.append((page_number, page_path, page_result))
            if not pending:
                continue

            transcriptions = await reader.transcribe(
                [
                    CampusNoticeImage(
                        title=f"文档第 {page_number} 张页面图像（{page_path.name}）",
                        data_url=_data_url(page_path),
                    )
                    for page_number, page_path, _ in pending
                ]
            )
            if len(transcriptions) != len(pending):
                raise RuntimeError(
                    f"Batch beginning at page {batch_start + 1} returned "
                    f"{len(transcriptions)} results for {len(pending)} pages"
                )
            for (page_number, _, page_result), transcription in zip(
                pending,
                transcriptions,
                strict=True,
            ):
                transcription = transcription.strip()
                if not transcription:
                    raise RuntimeError(f"Page {page_number} returned an empty transcription")
                _write_atomic(page_result, transcription + "\n")
                completed += 1
                print(
                    json.dumps(
                        {
                            "page": page_number,
                            "pages": len(pages),
                            "status": "completed",
                            "characters": len(transcription),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
    finally:
        await reader.close()

    sections = []
    for page_number in range(1, len(pages) + 1):
        page_result = work_directory / f"page-{page_number:04d}.txt"
        transcription = page_result.read_text(encoding="utf-8").strip()
        if not transcription:
            raise RuntimeError(f"Page {page_number} has no resumable transcription")
        sections.append(f"【PDF 第 {page_number} 页】\n{transcription}")
    assembled = "\n\n".join(sections) + "\n"
    _write_atomic(output, assembled)
    return {
        "output": str(output),
        "work_directory": str(work_directory),
        "pages": len(pages),
        "completed": completed,
        "reused": reused,
        "characters": len(assembled),
    }


def main() -> int:
    result = asyncio.run(_run(_arguments()))
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
