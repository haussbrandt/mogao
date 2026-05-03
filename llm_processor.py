import asyncio
import json
import os
import pickle
from datetime import datetime
from typing import Optional

import google.generativeai as genai  # type: ignore
from google.api_core import retry_async
from google.generativeai.types import RequestOptions
from pydantic import BaseModel

from constants import LIBRARY_PATH

CHUNK_SIZE_CHARS = 20_000
RATE_LIMIT_PER_MIN = 14  # It's actually 15, but sometimes API was complaining
MIN_INTERVAL_S = 60.0 / RATE_LIMIT_PER_MIN

# Global rate-limiter (shared across all concurrent book-processing tasks)
_rate_lock: Optional[asyncio.Lock] = None
_last_request_at: float = 0.0


def _get_rate_lock() -> asyncio.Lock:
    """Lazily create the asyncio.Lock inside an async context."""
    global _rate_lock
    if _rate_lock is None:
        _rate_lock = asyncio.Lock()
    return _rate_lock


def get_book_dict_path(book_id: str) -> str:
    safe_id = os.path.basename(book_id)
    return os.path.join(LIBRARY_PATH, safe_id, "book_dict.json")


def load_book_dict(book_id: str) -> dict:
    """Load book_dict.json, returning a safe default if missing/corrupt."""
    path = get_book_dict_path(book_id)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[book_dict] Load error {book_id}: {e}")
    return {"status": "none", "words": {}, "processed_chunks": 0, "total_chunks": 0}


def _save_book_dict(book_id: str, data: dict) -> None:
    path = get_book_dict_path(book_id)
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
- definition: a concise dictionary-style entry in English. For cultural, religious or domain-specific terms,
include a one-sentence explanation of what the concept acutally is - not just its English equivalent. For proper nouns,
briefly identify who or what it refers to within this work. Aim for the style of a learner's dictionary or encyclopedia gloss:
clear, informative, and under 40 words. For real people, include birth and death dates in parentheses. For fictional characters, include the source.

Examples:
Lovelace, Ada (1815–1852). English mathematician often cited as the first computer programmer for her work on CHARLES BABBAGE’S early mechanical general-purpose computer, the Analytical Engine.

Wu Zetian (624–705).
The only woman to rule China as an emperor in her own right (690-705). She founded the Zhou dynasty, expanded the empire, and significantly reformed the imperial examination system to favor meritocracy.

Lin Daiyu (c. 18th Century).
A central character in the Chinese classic Dream of the Red Chamber. Known for her poetic brilliance and emotional sensitivity, she represents the tragic "frail beauty" archetype in Chinese literature.


Do not output anything else. Do not justify your choices in the answer.

Text:
{text}
"""


async def _rate_limited_wait() -> None:
    global _last_request_at
    lock = _get_rate_lock()
    async with lock:
        now = asyncio.get_event_loop().time()
        wait = MIN_INTERVAL_S - (now - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = asyncio.get_event_loop().time()


async def _call_llm(model, text: str) -> list[dict]:
    """Send one chunk to the LLM and return a list of word-entry dicts."""
    response = await model.generate_content_async(
        _PROMPT.format(text=text),
        generation_config={
            "response_mime_type": "application/json",
            "response_schema": {
                "type": "OBJECT",
                "properties": {
                    "words": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "word": {"type": "STRING"},
                                "pinyin": {"type": "STRING"},
                                "english_meaning": {"type": "STRING"},
                            },
                            "required": ["word", "pinyin", "english_meaning"],
                        },
                    }
                },
                "required": ["words"],
            },
            "temperature": 0.1,
        },
        request_options=RequestOptions(
            timeout=600,
            retry=retry_async.AsyncRetry(
                initial=5, multiplier=2, maximum=60, timeout=300
            ),  # pyright: ignore[reportArgumentType]
        ),
    )
    try:
        data = json.loads(response.text)
        return data.get("words", [])
    except json.JSONDecodeError as e:
        print(f"[LLM] JSON Error: {e}")
        print("[LLM] Retrying with raw_decode")
        data, _ = json.JSONDecoder().raw_decode(response.text.strip())
    return data.get("words", [])


async def process_book_background(book_id: str, book, resume: bool = False) -> None:
    """
    Asyncio background task — do not await, use asyncio.create_task().

    Chunks the book spine, calls Gemma for each chunk (respecting the rate
    limit), and saves discovered words incrementally to book_dict.json so
    the reader can access them as soon as the first chunk is processed.

    Args:
        book_id:  the UUID folder name (e.g. "a1b2c3d4-...")
        book:     a fully-loaded Book dataclass
        resume:   if True, skip chunks already counted in processed_chunks
                  (used on server restart to recover interrupted jobs)
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print(f"[LLM] GEMINI_API_KEY not set — skipping book dict for {book_id}")
        return

    genai.configure(api_key=api_key)
    model_name = os.getenv("GEMMA_MODEL", "gemma-4-31b-it")
    model = genai.GenerativeModel(model_name)

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
    print(f"[LLM] {book_id}: {total} chunk(s), {len(completed_indices)} already done.")

    async def fetch_chunk(index: int, text: str):
        await _rate_limited_wait()
        print(
            f"[LLM] {book_id}: Sending chunk {index + 1}/{total} ({len(text):,} chars) to API..."
        )
        try:
            entries = await _call_llm(model, text)
            return index, entries, None
        except Exception as e:
            print(f"[LLM] {book_id}: Chunk {index + 1} permanently failed: {e}")
            return index, [], e

    tasks = [
        asyncio.create_task(fetch_chunk(i, chunk))
        for i, chunk in enumerate(chunks)
        if i not in completed_indices
    ]

    for coro in asyncio.as_completed(tasks):
        i, entries, error = await coro

        if error:
            print(
                f"[LLM] {book_id}: chunk {i + 1} permanently failed. Will retry on next server reboot."
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
        print(
            f"[LLM] {book_id}: chunk {i + 1}/{total} done"
            f" — {len(state['words'])} words so far"
        )

    if len(completed_indices) >= total:
        state["status"] = "done"
        state["completed_at"] = datetime.now().isoformat()
        _save_book_dict(book_id, state)
        print(f"[LLM] {book_id}: complete — {len(state['words'])} LLM words extracted")
    else:
        print(
            f"[LLM] {book_id}: paused with errors. {len(completed_indices)}/{total} chunks completed."
        )


async def resume_interrupted_processing() -> None:
    """
    Called once at server startup. Scans all book folders for any
    book_dict.json files whose status is still "processing" (meaning the
    server was shut down mid-run) and resumes them as background tasks.
    """
    if not os.path.exists(LIBRARY_PATH):
        return

    for book_id in os.listdir(LIBRARY_PATH):
        dict_path = get_book_dict_path(book_id)
        if not os.path.exists(dict_path):
            continue
        try:
            with open(dict_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("status") != "processing":
                continue
            pkl_path = os.path.join(LIBRARY_PATH, book_id, "book.pkl")
            if not os.path.exists(pkl_path):
                continue
            with open(pkl_path, "rb") as f:
                book = pickle.load(f)
            print(f"[LLM] Resuming interrupted processing for {book_id}")
            asyncio.create_task(process_book_background(book_id, book, resume=True))
        except Exception as e:
            print(f"[LLM] Could not resume {book_id}: {e}")
