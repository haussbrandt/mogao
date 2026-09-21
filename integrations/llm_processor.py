import asyncio
import json
import logging
import os
import pickle
import re
import tempfile
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
_dictionary_retry_tasks: set[asyncio.Task] = set()
_video_dictionary_tasks: dict[str, asyncio.Task] = {}


def cancel_video_dictionary_job(video_id: str) -> None:
    task = _video_dictionary_tasks.pop(str(video_id), None)
    if task is not None:
        task.cancel()


def schedule_video_dictionary_job(video_id: str, *, resume: bool = False) -> None:
    """Register before the job starts so even a queued job can be cancelled."""
    item_id = str(video_id)
    cancel_video_dictionary_job(item_id)
    task = asyncio.create_task(process_subtitles_background(item_id, resume=resume))
    _video_dictionary_tasks[item_id] = task

    def forget(completed: asyncio.Task) -> None:
        if _video_dictionary_tasks.get(item_id) is completed:
            _video_dictionary_tasks.pop(item_id)

    task.add_done_callback(forget)


def _error_summary(error: Exception) -> str:
    message = " ".join(str(error).split())
    return (message or type(error).__name__)[:300]


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


def _save_dict(path: str, data: dict) -> None:
    destination = os.fspath(path)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=os.path.dirname(destination) or ".",
            prefix=f"{os.path.basename(destination)}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            temporary_path = f.name
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary_path, destination)
    except Exception:
        if temporary_path is not None:
            try:
                os.remove(temporary_path)
            except FileNotFoundError:
                pass
        raise


def _save_book_dict(book_id: str, data: dict) -> None:
    _save_dict(get_book_dict_path(book_id), data)


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
    _save_dict(get_video_dict_path(video_id), data)


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
    for entry in data["words"]:
        if not isinstance(entry, dict) or any(
            not isinstance(entry.get(field), str)
            for field in ("word", "pinyin", "english_meaning")
        ):
            raise LLMResponseError("LLM response contained an invalid dictionary entry")
    return data["words"]


async def process_book_background(book_id: str, book, resume: bool = False) -> None:
    if not settings.dictionary_generation.enabled:
        return

    job_label = f"Book {book_id}"
    try:
        chunks = _chunk_spine(book.spine)
    except Exception as error:
        _record_job_failure(
            job_label, book_id, error, load_book_dict, _save_book_dict
        )
        return

    await _run_dictionary_job(
        job_label,
        book_id,
        chunks,
        load_book_dict,
        _save_book_dict,
        resume=resume,
    )


async def process_subtitles_background(video_id, resume: bool = False) -> None:
    if not settings.dictionary_generation.enabled:
        return

    item_id = str(video_id)
    job_label = f"Video {item_id}"
    try:
        with open(get_video_path(item_id, "subtitles.srt"), encoding="utf-8") as f:
            chunks = _chunk_subtitles(f.read())
    except Exception as error:
        _record_job_failure(
            job_label, item_id, error, load_video_dict, _save_video_dict
        )
        return

    await _run_dictionary_job(
        job_label,
        item_id,
        chunks,
        load_video_dict,
        _save_video_dict,
        resume=resume,
    )


def _record_job_failure(
    job_label: str, item_id: str, error: Exception, load_state, save_state
) -> None:
    logger.exception(f"Dictionary generation failed for {job_label}")
    try:
        state = load_state(item_id)
        state["status"] = "error"
        state["last_error"] = _error_summary(error)
        state["updated_at"] = datetime.now().isoformat()
        save_state(item_id, state)
    except Exception:
        logger.exception(f"Could not save dictionary failure for {job_label}")


async def _run_dictionary_job(
    job_label: str,
    item_id: str,
    chunks: list[str],
    load_state,
    save_state,
    *,
    resume: bool = False,
) -> None:
    tasks = []
    try:
        api_key = os.environ["DICTIONARY_GENERATION_API_KEY"]
        total = len(chunks)
        state = (
            load_state(item_id)
            if resume
            else {
                "words": {},
                "completed_indices": [],
                "started_at": datetime.now().isoformat(),
            }
        )
        completed = set(state.get("completed_indices", []))
        state.update(
            status="processing",
            total_chunks=total,
            processed_chunks=len(completed),
            failed_chunks=[],
            updated_at=datetime.now().isoformat(),
        )
        state.pop("last_error", None)
        state.pop("completed_at", None)
        save_state(item_id, state)

        async def fetch_chunk(index: int, text: str):
            try:
                await _rate_limited_wait()
                logger.info(
                    f"{job_label}: sending chunk {index + 1}/{total} "
                    f"({len(text):,} characters) to the API"
                )
                entries = await _call_llm(
                    base_url=settings.dictionary_generation.base_url,
                    api_key=api_key,
                    model=settings.dictionary_generation.llm,
                    text=text,
                )
                return index, entries, None
            except Exception as error:
                logger.exception(f"{job_label}: chunk {index + 1} failed")
                return index, [], error

        tasks = [
            asyncio.create_task(fetch_chunk(i, chunk))
            for i, chunk in enumerate(chunks)
            if i not in completed
        ]
        for result in asyncio.as_completed(tasks):
            index, entries, error = await result
            if error is not None:
                state["failed_chunks"].append(index + 1)
                state["last_error"] = _error_summary(error)
            else:
                for entry in entries:
                    word = entry["word"].strip()
                    if word and word not in state["words"]:
                        state["words"][word] = {
                            "e": [
                                {
                                    "p": entry["pinyin"],
                                    "d": [entry["english_meaning"]],
                                }
                            ],
                            "llm": True,
                        }
                completed.add(index)
                state["completed_indices"] = sorted(completed)
                state["processed_chunks"] = len(completed)
            state["updated_at"] = datetime.now().isoformat()
            save_state(item_id, state)
            logger.info(
                f"{job_label}: {len(completed)}/{total} chunks complete"
            )

        state["status"] = "error" if state["failed_chunks"] else "done"
        state["updated_at"] = datetime.now().isoformat()
        if state["status"] == "done":
            state["completed_at"] = state["updated_at"]
        else:
            logger.warning(
                f"{job_label}: dictionary paused with errors; "
                "retry it from the status page or restart the server"
            )
        save_state(item_id, state)
    except Exception as error:
        # Reload the last checkpoint so an interrupted merge is never saved as
        # a successfully processed chunk.
        _record_job_failure(job_label, item_id, error, load_state, save_state)
    finally:
        # A failed or cancelled parent must not leave requests running after it.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _mark_dictionary_retry_started(item_id: str, state: dict, save_state) -> None:
    state["status"] = "processing"
    state["failed_chunks"] = []
    state.pop("last_error", None)
    state["updated_at"] = datetime.now().isoformat()
    save_state(item_id, state)


def _track_dictionary_retry(retry) -> None:
    task = asyncio.create_task(retry)
    _dictionary_retry_tasks.add(task)
    task.add_done_callback(_dictionary_retry_tasks.discard)


def schedule_book_dictionary_retry(book_id: str) -> bool:
    if not settings.dictionary_generation.enabled:
        return False

    state = load_book_dict(book_id)
    if state.get("status") != "error":
        return False

    book_path = get_book_path(book_id, "book.pkl")
    with open(book_path, "rb") as file:
        book = pickle.load(file)

    _mark_dictionary_retry_started(book_id, state, _save_book_dict)
    _track_dictionary_retry(process_book_background(book_id, book, resume=True))
    return True


def schedule_video_dictionary_retry(video_id: str) -> bool:
    if not settings.dictionary_generation.enabled or not settings.video.enabled:
        return False

    state = load_video_dict(video_id)
    if state.get("status") != "error":
        return False

    subtitles_path = get_video_path(video_id, "subtitles.srt")
    if not os.path.isfile(subtitles_path):
        raise FileNotFoundError(subtitles_path)

    _mark_dictionary_retry_started(video_id, state, _save_video_dict)
    schedule_video_dictionary_job(video_id, resume=True)
    return True


async def resume_interrupted_processing(*, include_videos: bool = True) -> None:
    """
    Called once at server startup. Resumes interrupted or paused dictionary jobs,
    including missing or corrupt dictionary state, for both books and videos.
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
                if data.get("status") not in {"processing", "error"}:
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
            if not os.path.isfile(get_video_path(video_id, "subtitles.srt")):
                continue
            dict_path = get_video_dict_path(video_id)
            try:
                with open(dict_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("status") not in {"processing", "error"}:
                    continue
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            try:
                logger.info(f"Resuming interrupted dictionary for video {video_id}")
                schedule_video_dictionary_job(video_id, resume=True)
            except Exception:
                logger.exception(f"Could not resume dictionary for video {video_id}")
    except FileNotFoundError:
        pass
