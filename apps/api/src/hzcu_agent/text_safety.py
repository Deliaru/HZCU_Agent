import unicodedata
from typing import Any


def clean_product_text(value: str) -> str:
    """Remove control characters that cannot be rendered or serialized safely."""

    return "".join(
        character
        for character in value
        if character in {"\n", "\r", "\t"} or unicodedata.category(character) not in {"Cc", "Cs"}
    )


def clean_product_json(value: Any) -> Any:
    """Recursively clean strings in JSON-shaped product payloads."""

    if isinstance(value, str):
        return clean_product_text(value)
    if isinstance(value, list):
        return [clean_product_json(item) for item in value]
    if isinstance(value, tuple):
        return [clean_product_json(item) for item in value]
    if isinstance(value, dict):
        return {
            clean_product_text(str(key)): clean_product_json(item) for key, item in value.items()
        }
    return value
