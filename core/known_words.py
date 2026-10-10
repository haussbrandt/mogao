"""Locally marked vocabulary, shared by the reader and coverage charts."""

import json
import os
import tempfile
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from core.config import settings

router = APIRouter(prefix="/api/known-words")
_lock = threading.RLock()


def load_local_vocabulary() -> dict:
    """Read words and their revision together, including legacy word lists."""
    with _lock:
        path = settings.paths.library / "known_words.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = []
        if isinstance(state, list):
            state = {"words": state, "revision": 0}
        if not isinstance(state, dict):
            raise ValueError("Invalid local vocabulary file")
        words = state.get("words")
        revision = state.get("revision")
        if (
            not isinstance(words, list)
            or any(not isinstance(word, str) for word in words)
            or type(revision) is not int
            or revision < 0
        ):
            raise ValueError("Invalid local vocabulary file")
        return {"words": sorted(set(words)), "revision": revision}


def load_local_known_words() -> set[str]:
    return set(load_local_vocabulary()["words"])


def set_local_known(word: str, known: bool) -> dict:
    with _lock:
        state = load_local_vocabulary()
        words = set(state["words"])
        if (word in words) == known:
            return state
        words.add(word) if known else words.discard(word)
        state = {"words": sorted(words), "revision": state["revision"] + 1}
        root = settings.paths.library
        root.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=root,
                prefix=".known-words-",
                delete=False,
            ) as file:
                temporary = file.name
                json.dump(state, file, ensure_ascii=False)
            os.replace(temporary, root / "known_words.json")
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)
        return state


class KnownWordUpdate(BaseModel):
    word: str = Field(min_length=1, max_length=128)
    known: bool

    @field_validator("word")
    @classmethod
    def clean_word(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("Enter a word")
        return value


@router.post("")
def update_known_word(update: KnownWordUpdate):
    if not settings.anki.enabled:
        raise HTTPException(404, "Known words are disabled")
    return set_local_known(update.word, update.known)


@router.get("")
def get_known_words():
    if not settings.anki.enabled:
        raise HTTPException(404, "Known words are disabled")
    return JSONResponse(load_local_vocabulary(), headers={"Cache-Control": "no-store"})
