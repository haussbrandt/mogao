"""Content versions for browser assets that must be deployed together."""

import hashlib
from pathlib import Path


STATIC_DIRECTORY = Path(__file__).resolve().parents[1] / "static"


def asset_version(*names: str) -> str:
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode() + b"\0")
        digest.update((STATIC_DIRECTORY / name).read_bytes())
    return digest.hexdigest()[:16]
