from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LocalOpenAIConfig:
    api_key: str
    base_url: str | None = None


def load_local_openai_config(path: str | Path) -> LocalOpenAIConfig:
    """Load an OpenAI-compatible key and optional base URL without logging them."""
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f"model config file does not exist: {config_path}")

    values: dict[str, str] = {}
    positional: list[str] = []
    for raw_line in config_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line and not line.lower().startswith(("http://", "https://")):
            key, value = line.split("=", 1)
            normalized_key = key.strip().lower().removeprefix("hzcu_")
            values[normalized_key] = value.strip().strip("\"'")
        else:
            positional.append(line.strip("\"'"))

    base_url = (
        values.get("openai_base_url")
        or values.get("base_url")
        or next(
            (item for item in positional if item.lower().startswith(("http://", "https://"))),
            None,
        )
    )
    api_key = values.get("openai_api_key") or values.get("api_key")
    if api_key is None:
        api_key = next(
            (item for item in positional if not item.lower().startswith(("http://", "https://"))),
            None,
        )
    if not api_key:
        raise ValueError(f"model config file does not contain an API key: {config_path.name}")
    return LocalOpenAIConfig(api_key=api_key, base_url=base_url)
