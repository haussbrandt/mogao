import asyncio
import json
import logging
import os
import pickle
import re
from datetime import datetime
from typing import Optional

from core.config import settings
from core.paths import (
    get_book_dict_path,
    get_book_path,
    get_video_dict_path,
    get_video_path,
)
from integrations.llm_client import LLMResponseError, generate_json

logger = logging.getLogger(__name__)

CHUNK_SIZE_CHARS = settings.dictionary_generation.chunk_size
RATE_LIMIT_PER_MIN = settings.dictionary_generation.requests_per_minute
MIN_INTERVAL_S = 60.0 / RATE_LIMIT_PER_MIN

# Global rate-limiter (shared across all concurrent processing tasks)
_rate_lock: Optional[asyncio.Lock] = None
_last_request_at: float = 0.0


def _get_rate_lock() -> asyncio.Lock:
    """Lazily create the asyncio.Lock inside an async context."""
    global _rate_lock
    if _rate_lock is None:
        _rate_lock = asyncio.Lock()
    return _rate_lock


def load_book_dict(book_id: str) -> dict:
    """Load book_dict.json, returning a safe default if missing/corrupt."""
    path = get_book_dict_path(book_id)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        logger.exception(f"Error loading dictionary for book {book_id}")
    return {"status": "none", "words": {}, "processed_chunks": 0, "total_chunks": 0}


def _save_book_dict(book_id: str, data: dict) -> None:
    path = get_book_dict_path(book_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


def load_video_dict(video_id: str) -> dict:
    """Load subtitles_dict.json, returning a safe default if missing/corrupt."""
    path = get_video_dict_path(video_id)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        logger.exception(f"Error loading dictionary for video {video_id}")
    return {"status": "none", "words": {}, "processed_chunks": 0, "total_chunks": 0}


def _save_video_dict(video_id: str, data: dict) -> None:
    path = get_video_dict_path(video_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


# ──────────────────────────────────────────────────────────────
# Text chunking — groups chapters into ~CHUNK_SIZE_CHARS blocks
# ──────────────────────────────────────────────────────────────


def _chunk_spine(spine) -> list[str]:
    """
    Walk the spine and accumulate chapter text until CHUNK_SIZE_CHARS
    Chinese characters are reached, then start a new chunk.
    Chapters are never split mid-way — boundaries fall between chapters.
    """
    chunks: list[str] = []
    current_texts: list[str] = []
    current_count: int = 0

    for chapter in spine:
        ch_chars = sum(1 for c in chapter.text if "\u4e00" <= c <= "\u9fff")
        if current_count + ch_chars > CHUNK_SIZE_CHARS and current_texts:
            chunks.append("\n\n".join(current_texts))
            current_texts = [chapter.text]
            current_count = ch_chars
        else:
            current_texts.append(chapter.text)
            current_count += ch_chars

    if current_texts:
        chunks.append("\n\n".join(current_texts))

    return chunks


def _chunk_subtitles(subtitles) -> list[str]:
    """
    Walk the subtitle lines and accumulate text until CHUNK_SIZE_CHARS
    Chinese characters are reached, then start a new chunk.
    Lines are never split mid-way — boundaries fall between lines.
    """
    chunks: list[str] = []
    current_texts: list[str] = []
    current_count: int = 0

    _SRT_METADATA = re.compile(
        r"^\d+\s*$"  # sequence number lines
        r"|^\d{2}:\d{2}:\d{2},\d{3}\s*-->"  # timestamp lines
        r"|<[^>]+>",  # inline tags
        re.MULTILINE,
    )

    lines = []
    for line in subtitles.splitlines():
        cleaned = _SRT_METADATA.sub("", line).strip()
        if cleaned:
            lines.append(cleaned)

    for line in lines:
        ch_chars = sum(1 for c in line if "\u4e00" <= c <= "\u9fff")
        if current_count + ch_chars > CHUNK_SIZE_CHARS and current_texts:
            chunks.append("\n".join(current_texts))
            current_texts = [line]
            current_count = ch_chars
        else:
            current_texts.append(line)
            current_count += ch_chars

    if current_texts:
        chunks.append("\n".join(current_texts))

    return chunks


_PROMPT = """\
You are a Chinese-language expert helping a reader study a Chinese novel.

Analyze the following text excerpt and extract:
1. Proper nouns — character names, place names, titles, factions, organizations,
and any named concepts specific to this work. For names, include first name, last name and full name.
2. Uncommon or domain-specific vocabulary that would not appear in the CC-CEDICT.

Do NOT include everyday common words that any intermediate learner would know.

For each item provide:
- word: the simplified Chinese
- pinyin: romanization with tone marks (e.g. "Wáng Míng", "qīngōng"). Do not put spaces within a word - Dursley = Désīlǐ.
- english_meaning: a concise dictionary-style entry in English. For cultural, religious or domain-specific terms,
include a one-sentence explanation of what the concept actually is - not just its English equivalent.
Aim for the style of a learner's dictionary or encyclopedia gloss:
clear, informative, and under 40 words. For real people, include birth and death dates in parentheses.

A definition MUST be valid, even if the context is unavailable or the phrase appears in a completely unrelated context.
A definition MUST NOT summarize, interpret, or refer to anything that happens in the excerpt or elsewhere in the story.
A defintion MUST be context-independent.

GOOD: A female given name.
BAD: The protagonist's given name.

Do not identify the character’s role, relationships, importance, actions, or circumstances.
Once the general dictionary meaning has been given, end the definition immediately. Never append the item’s use, function, ownership, origin, or significance in the excerpt.

Do not put spoilers in the definitions. Do not be overly specific.
BAD: 克劳迪 - Claudius: The villainous King of Denmark who secretly murdered his brother, King Hamlet, by pouring poison into his ear while he was sleeping in the garden. He marries the Queen and is later stabbed and poisoned by Hamlet.

BAD: 克劳迪 - Claudius: The ruler of Denmark who likes to throw loud drinking parties, gets nervous during a play called *The Mousetrap*, and is seen trying to pray alone in his chapel.

GOOD: 克劳迪 - Claudius: Given name derived from latin *claudus* ("lame" or "enclosed").

Examples:
Lovelace, Ada (1815–1852). English mathematician often cited as the first computer programmer.

Wu Zetian (624–705).
The only woman to rule China as an emperor in her own right (690-705).

Lin Daiyu (c. 18th Century).
A fictional character in the Chinese classic Dream of the Red Chamber.


Do not output anything else. Do not justify your choices in the answer.

Text:
{text}
"""

_DICTIONARY_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "words": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "word": {"type": "string"},
                    "pinyin": {"type": "string"},
                    "english_meaning": {"type": "string"},
                },
                "required": ["word", "pinyin", "english_meaning"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["words"],
    "additionalProperties": False,
}


async def _rate_limited_wait() -> None:
    global _last_request_at
    lock = _get_rate_lock()
    async with lock:
        now = asyncio.get_event_loop().time()
        wait = MIN_INTERVAL_S - (now - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = asyncio.get_event_loop().time()


async def _call_llm(
    *, base_url: str, api_key: str, model: str, text: str
) -> list[dict]:
    """Send one chunk to the LLM and return a list of word-entry dicts."""
    data = await generate_json(
        base_url=base_url,
        api_key=api_key,
        model=model,
        prompt=_PROMPT.format(text=text),
        response_schema=_DICTIONARY_RESPONSE_SCHEMA,
        schema_name="dictionary_words",
        temperature=0.1,
        timeout=600,
    )
    if not isinstance(data, dict) or not isinstance(data.get("words"), list):
        raise LLMResponseError("LLM response did not contain a words array")
    return data["words"]


async def process_book_background(book_id: str, book, resume: bool = False) -> None:
    """
    Asyncio background task — do not await, use asyncio.create_task().

    Chunks the book spine, calls the configured LLM for each chunk (respecting
    the rate limit), and saves discovered words incrementally to book_dict.json
    so the reader can access them as soon as the first chunk is processed.

    Args:
        book_id:  the UUID folder name (e.g. "a1b2c3d4-...")
        book:     a fully-loaded Book dataclass
        resume:   if True, skip chunks already present in completed_indices
                  (used on server restart to recover interrupted jobs)
    """
    if not settings.dictionary_generation.enabled:
        return

    api_key = os.environ["DICTIONARY_GENERATION_API_KEY"]
    model_name = settings.dictionary_generation.llm
    base_url = settings.dictionary_generation.base_url

    chunks = _chunk_spine(book.spine)
    total = len(chunks)

    if resume:
        state = load_book_dict(book_id)
        completed_indices = set(state.get("completed_indices", []))
        if len(completed_indices) >= total:
            state["status"] = "done"
            _save_book_dict(book_id, state)
            return

        state["status"] = "processing"
        state["total_chunks"] = total
    else:
        completed_indices = set()
        state: dict = {
            "status": "processing",
            "words": {},
            "processed_chunks": 0,
            "completed_indices": [],
            "total_chunks": total,
            "started_at": datetime.now().isoformat(),
        }

    _save_book_dict(book_id, state)
    logger.info(
        f"Book {book_id}: {total} chunks, {len(completed_indices)} already complete"
    )

    async def fetch_chunk(index: int, text: str):
        await _rate_limited_wait()
        logger.info(
            f"Book {book_id}: sending chunk {index + 1}/{total} "
            f"({len(text):,} characters) to the API"
        )
        try:
            entries = await _call_llm(
                base_url=base_url,
                api_key=api_key,
                model=model_name,
                text=text,
            )
            return index, entries, None
        except Exception as error:
            logger.exception(f"Book {book_id}: chunk {index + 1} failed")
            return index, [], error

    tasks = [
        asyncio.create_task(fetch_chunk(i, chunk))
        for i, chunk in enumerate(chunks)
        if i not in completed_indices
    ]

    for coro in asyncio.as_completed(tasks):
        i, entries, error = await coro

        if error:
            logger.warning(
                f"Book {book_id}: chunk {i + 1} will be retried after the next "
                "server restart"
            )
            continue

        for entry in entries:
            w = (entry.get("word") or "").strip()
            if not w:
                continue
            if w not in state["words"]:
                state["words"][w] = {
                    "e": [
                        {
                            "p": entry.get("pinyin", ""),
                            "d": [entry.get("english_meaning", "")],
                        }
                    ],
                    "llm": True,
                }

        completed_indices.add(i)
        state["completed_indices"] = list(completed_indices)
        state["processed_chunks"] = len(completed_indices)

        _save_book_dict(book_id, state)
        logger.info(
            f"Book {book_id}: chunk {i + 1}/{total} complete; "
            f"{len(state['words'])} words so far"
        )

    if len(completed_indices) >= total:
        state["status"] = "done"
        state["completed_at"] = datetime.now().isoformat()
        _save_book_dict(book_id, state)
        logger.info(
            f"Book {book_id}: dictionary complete; "
            f"{len(state['words'])} LLM words extracted"
        )
    else:
        logger.warning(
            f"Book {book_id}: dictionary paused with errors; "
            f"{len(completed_indices)}/{total} chunks complete"
        )


async def process_subtitles_background(video_id, resume: bool = False) -> None:
    """
    Asyncio background task — do not await, use asyncio.create_task().

    Chunks the subtitles, calls the configured LLM for each chunk (respecting
    the rate limit), and saves discovered words incrementally to
    subtitles_dict.json so the user can access them as soon as the first chunk
    is processed.

    Args:
        video_id:  the UUID folder name (e.g. "a1b2c3d4-...")
        resume:    if True, skip chunks already present in completed_indices
                   (used on server restart to recover interrupted jobs)
    """
    if not settings.dictionary_generation.enabled:
        return

    api_key = os.environ["DICTIONARY_GENERATION_API_KEY"]
    model_name = settings.dictionary_generation.llm
    base_url = settings.dictionary_generation.base_url

    subtitles_path = get_video_path(video_id, "subtitles.srt")
    if not os.path.exists(subtitles_path):
        return

    with open(subtitles_path, "r") as f:
        subtitles = f.read()
    chunks = _chunk_subtitles(subtitles)
    total = len(chunks)

    if resume:
        state = load_video_dict(video_id)
        completed_indices = set(state.get("completed_indices", []))
        if len(completed_indices) >= total:
            state["status"] = "done"
            _save_video_dict(video_id, state)
            return

        state["status"] = "processing"
        state["total_chunks"] = total
    else:
        completed_indices = set()
        state: dict = {
            "status": "processing",
            "words": {},
            "processed_chunks": 0,
            "completed_indices": [],
            "total_chunks": total,
            "started_at": datetime.now().isoformat(),
        }

    _save_video_dict(video_id, state)
    logger.info(
        f"Video {video_id}: {total} chunks, {len(completed_indices)} already complete"
    )

    async def fetch_chunk(index: int, text: str):
        await _rate_limited_wait()
        logger.info(
            f"Video {video_id}: sending chunk {index + 1}/{total} "
            f"({len(text):,} characters) to the API"
        )
        try:
            entries = await _call_llm(
                base_url=base_url,
                api_key=api_key,
                model=model_name,
                text=text,
            )
            return index, entries, None
        except Exception as error:
            logger.exception(f"Video {video_id}: chunk {index + 1} failed")
            return index, [], error

    tasks = [
        asyncio.create_task(fetch_chunk(i, chunk))
        for i, chunk in enumerate(chunks)
        if i not in completed_indices
    ]

    for coro in asyncio.as_completed(tasks):
        i, entries, error = await coro

        if error:
            logger.warning(
                f"Video {video_id}: chunk {i + 1} will be retried after the next "
                "server restart"
            )
            continue

        for entry in entries:
            w = (entry.get("word") or "").strip()
            if not w:
                continue
            if w not in state["words"]:
                state["words"][w] = {
                    "e": [
                        {
                            "p": entry.get("pinyin", ""),
                            "d": [entry.get("english_meaning", "")],
                        }
                    ],
                    "llm": True,
                }

        completed_indices.add(i)
        state["completed_indices"] = list(completed_indices)
        state["processed_chunks"] = len(completed_indices)

        _save_video_dict(video_id, state)
        logger.info(
            f"Video {video_id}: chunk {i + 1}/{total} complete; "
            f"{len(state['words'])} words so far"
        )

    if len(completed_indices) >= total:
        state["status"] = "done"
        state["completed_at"] = datetime.now().isoformat()
        _save_video_dict(video_id, state)
        logger.info(
            f"Video {video_id}: dictionary complete; "
            f"{len(state['words'])} LLM words extracted"
        )
    else:
        logger.warning(
            f"Video {video_id}: dictionary paused with errors; "
            f"{len(completed_indices)}/{total} chunks complete"
        )


async def resume_interrupted_processing(*, include_videos: bool = True) -> None:
    """
    Called once at server startup. Scans all book folders and resumes processing when
    book_dict.json still has "processing" status (meaning the
    server was shut down mid-run), is missing or corrupt.
    Also does the same for video folders and subtitles_dict.json files.
    """
    if not settings.dictionary_generation.enabled:
        return

    # TODO: Refactor
    try:
        for book_id in os.listdir(settings.paths.library):
            try:
                book_path = get_book_path(book_id)
            except ValueError:
                continue
            if not os.path.isdir(book_path):
                continue
            pkl_path = get_book_path(book_id, "book.pkl")
            if not os.path.exists(pkl_path):
                continue
            dict_path = get_book_dict_path(book_id)
            try:
                with open(dict_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("status") != "processing":
                    continue
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            try:
                with open(pkl_path, "rb") as f:
                    book = pickle.load(f)
                logger.info(f"Resuming interrupted dictionary for book {book_id}")
                asyncio.create_task(process_book_background(book_id, book, resume=True))
            except Exception:
                logger.exception(f"Could not resume dictionary for book {book_id}")
    except FileNotFoundError:
        pass
    if not include_videos:
        return

    try:
        for video_id in os.listdir(settings.paths.video_library):
            try:
                video_path = get_video_path(video_id)
            except ValueError:
                continue
            if not os.path.isdir(video_path):
                continue
            dict_path = get_video_dict_path(video_id)
            try:
                with open(dict_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("status") != "processing":
                    continue
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            try:
                logger.info(f"Resuming interrupted dictionary for video {video_id}")
                asyncio.create_task(process_subtitles_background(video_id, resume=True))
            except Exception:
                logger.exception(f"Could not resume dictionary for video {video_id}")
    except FileNotFoundError:
        pass
