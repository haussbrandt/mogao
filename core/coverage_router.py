import asyncio
import logging
import threading
from collections import OrderedDict
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse

from core.config import settings
from core.coverage import (
    ensure_vocabulary_stats,
    item_paths,
    refresh_coverage,
    score_vocabulary_stats,
)
from core.known_words import load_local_vocabulary
from integrations.anki import get_anki_word_sets

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/coverage")
Kind = Literal["books", "videos"]
_snapshots: OrderedDict[str, frozenset[str]] = OrderedDict()
_snapshot_lock = threading.Lock()
SNAPSHOT_LIMIT = 16
_latest_snapshot: str | None = None
_refresh_lock = threading.Lock()


def _known_words(kind: str, snapshot: UUID | None = None, *, refresh=False):
    global _latest_snapshot
    if kind == "videos" and not settings.video.enabled:
        raise HTTPException(404, "Videos are disabled")
    if not settings.anki.enabled:
        raise HTTPException(409, "Enable Anki to see your vocabulary coverage.")
    if snapshot is not None:
        with _snapshot_lock:
            words = _snapshots.get(str(snapshot))
            if words is None:
                raise HTTPException(
                    409, "Vocabulary snapshot expired. Refresh vocabulary."
                )
            _snapshots.move_to_end(str(snapshot))
        return str(snapshot), words, False
    with _snapshot_lock:
        previous = _latest_snapshot
        if not refresh and previous in _snapshots:
            return previous, _snapshots[previous], True
    # Concurrent page loads share a single Anki sync.
    with _refresh_lock:
        with _snapshot_lock:
            if _latest_snapshot != previous and _latest_snapshot in _snapshots:
                return _latest_snapshot, _snapshots[_latest_snapshot], False
        try:
            words = frozenset(get_anki_word_sets()[1])
        except Exception:
            logger.exception("Could not load known vocabulary for coverage")
            raise HTTPException(503, "Could not refresh known words from Anki. Try again.")
        token = str(uuid4())
        with _snapshot_lock:
            _snapshots[token] = words
            _latest_snapshot = token
            while len(_snapshots) > SNAPSHOT_LIMIT:
                _snapshots.popitem(last=False)
        return token, words, False


def _score(kind: str, item_id: str, known: set[str], *, detail=False, build=True):
    try:
        vocabulary_stats, status = ensure_vocabulary_stats(kind, item_id, build=build)
        return (
            score_vocabulary_stats(vocabulary_stats, known, detail=detail)
            if vocabulary_stats
            else {"status": status}
        )
    except Exception:
        logger.exception("Could not score %s %s", kind, item_id)
        return {"status": "error"}


def _library_scores(kind: str, snapshot: UUID | None = None, refresh=False):
    token, anki_words, cached = _known_words(kind, snapshot, refresh=refresh)
    local = load_local_vocabulary()
    known = anki_words | set(local["words"])
    root = settings.paths.library if kind == "books" else settings.paths.video_library
    result = {}
    if root.is_dir():
        for folder in root.iterdir():
            try:
                item_id = str(UUID(folder.name))
            except ValueError:
                continue
            metadata = folder / ("book.pkl" if kind == "books" else "video.pkl")
            if metadata.is_file():
                result[item_id] = _score(kind, item_id, known, build=False)
    return {
        "snapshot": token,
        "local_words": local["words"],
        "local_revision": local["revision"],
        "items": result,
        "cached_vocabulary": cached,
    }


@router.get("/{kind}")
async def library_coverage(
    kind: Kind,
    background_tasks: BackgroundTasks,
    snapshot: UUID | None = None,
    refresh: bool = False,
):
    result = await asyncio.to_thread(_library_scores, kind, snapshot, refresh)
    for item_id, score in result["items"].items():
        if score["status"] == "analysing":
            background_tasks.add_task(refresh_coverage, kind, item_id)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/{kind}/{item_id}")
async def item_coverage(kind: Kind, item_id: UUID, snapshot: UUID | None = None):
    def load():
        folder, _, _ = item_paths(kind, str(item_id))
        if not folder.is_dir():
            raise HTTPException(404, "Item not found")
        token, anki_words, _ = _known_words(kind, snapshot)
        local = load_local_vocabulary()
        return {
            **_score(kind, str(item_id), anki_words | set(local["words"]), detail=True),
            "snapshot": token,
            "local_words": local["words"],
            "local_revision": local["revision"],
        }

    return JSONResponse(
        await asyncio.to_thread(load), headers={"Cache-Control": "no-store"}
    )
