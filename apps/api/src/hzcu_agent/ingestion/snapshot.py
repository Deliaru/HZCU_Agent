import asyncio
import gzip
import hashlib
import os
from pathlib import Path
from uuid import uuid4


class SnapshotStore:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    async def put(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        destination = self._root / digest[:2] / f"{digest}.gz"
        if not destination.exists():
            await asyncio.to_thread(self._write_atomic, destination, content)
        return f"snapshot://sha256/{digest}"

    def path_for_uri(self, uri: str) -> Path:
        prefix = "snapshot://sha256/"
        if not uri.startswith(prefix):
            raise ValueError("Unsupported snapshot URI")
        digest = uri.removeprefix(prefix)
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("Invalid snapshot digest")
        return self._root / digest[:2] / f"{digest}.gz"

    @staticmethod
    def _write_atomic(destination: Path, content: bytes) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
        try:
            with gzip.open(temporary, "wb", compresslevel=6) as handle:
                handle.write(content)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
