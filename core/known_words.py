"""Locally marked vocabulary, shared by the reader and coverage charts."""

import json
import logging
import os
import tempfile
import threading
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from core.config import settings

router = APIRouter(prefix="/api/known-words")
_lock = threading.RLock()
logger = logging.getLogger(__name__)


class DictionaryEntry(BaseModel):
    p: str = Field(default="", max_length=256)
    d: list[Annotated[str, Field(max_length=10000)]] = Field(min_length=1, max_length=128)


class WordDefinition(BaseModel):
    e: list[DictionaryEntry] = Field(min_length=1, max_length=128)
    llm: bool = False


def load_generated_word_definitions(known: set[str]) -> dict:
    """Read supplementary dictionary entries for the requested words."""
    result = {}
    if not known:
        return result
    for root, filename in (
        (settings.paths.library, "book_dict.json"),
        (settings.paths.video_library, "subtitles_dict.json"),
    ):
        if not root.is_dir():
            continue
        for folder in sorted(root.iterdir()):
            try:
                UUID(folder.name)
            except ValueError:
                continue
            path = folder / filename
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                logger.warning("Could not load generated dictionary %s", path)
                continue
            words = data.get("words") if isinstance(data, dict) else None
            if not isinstance(words, dict):
                continue
            for word in known.intersection(words):
                raw = words[word]
                entries = raw.get("e") if isinstance(raw, dict) else None
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if (
                        not isinstance(entry, dict)
                        or not isinstance(entry.get("p", ""), str)
                        or not isinstance(entry.get("d"), list)
                        or not entry["d"]
                        or any(not isinstance(d, str) for d in entry["d"])
                    ):
                        continue
                    cleaned = {"p": entry.get("p", ""), "d": entry["d"]}
                    target = result.setdefault(word, {"e": []})["e"]
                    if cleaned not in target:
                        target.append(cleaned)
    return result


def load_known_word_definitions(state: dict | None = None) -> dict:
    state = load_local_vocabulary() if state is None else state
    saved = {word: entry for word, entry in state["words"].items() if entry is not None}
    fallback = load_generated_word_definitions(set(state["words"]) - saved.keys())
    return {**fallback, **saved}


def load_local_vocabulary() -> dict:
    """Read known words, their optional saved entries, and their revision."""
    with _lock:
        path = settings.paths.library / "known_words.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {"words": {}, "revision": 0}
        if not isinstance(state, dict):
            raise ValueError("Invalid local vocabulary file")
        words = state.get("words")
        revision = state.get("revision")
        if (
            not isinstance(words, dict)
            or any(not isinstance(word, str) for word in words)
            or type(revision) is not int
            or revision < 0
        ):
            raise ValueError("Invalid local vocabulary file")
        words = {
            word: WordDefinition.model_validate(raw).model_dump() if raw is not None else None
            for word, raw in sorted(words.items())
        }
        return {"words": words, "revision": revision}


def load_local_known_words() -> set[str]:
    return set(load_local_vocabulary()["words"])


def write_local_vocabulary(state: dict) -> None:
    """Atomically save a snapshot; callers must prevent concurrent writes."""
    root = settings.paths.library
    root.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=root, prefix=".known-words-", delete=False,
        ) as file:
            temporary = file.name
            json.dump(state, file, ensure_ascii=False)
        os.replace(temporary, root / "known_words.json")
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def set_local_known(word: str, known: bool, definition: WordDefinition | None = None) -> dict:
    with _lock:
        state = load_local_vocabulary()
        words = state["words"]
        previous = dict(words)
        if known:
            if definition is not None:
                words[word] = definition.model_dump()
            else:
                words.setdefault(word, None)
        else:
            words.pop(word, None)
        if words == previous:
            return state
        state["words"] = dict(sorted(words.items()))
        state["revision"] += 1
        write_local_vocabulary(state)
        return state


class KnownWordUpdate(BaseModel):
    word: str = Field(min_length=1, max_length=128)
    known: bool
    definition: WordDefinition | None = None

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
    return set_local_known(update.word, update.known, update.definition)


@router.get("")
def get_known_words():
    if not settings.anki.enabled:
        raise HTTPException(404, "Known words are disabled")
    return JSONResponse(load_local_vocabulary(), headers={"Cache-Control": "no-store"})


@router.get("/definitions")
def get_known_word_definitions():
    if not settings.anki.enabled:
        raise HTTPException(404, "Known words are disabled")
    state = load_local_vocabulary()
    return JSONResponse(
        {"words": load_known_word_definitions(state),
         "saved_words": [word for word, entry in state["words"].items() if entry is not None]},
        headers={"Cache-Control": "no-store"},
    )
