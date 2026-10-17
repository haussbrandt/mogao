"""Persistent vocabulary statistics for texts and live learner coverage.

Segmentation mirrors static/segmenter.js with an empty known-word set. Keep the
parity test when changing either implementation; bump VOCABULARY_STATS_VERSION
if word boundaries, extraction, or classification rules change.
"""

import asyncio
import json
import logging
import math
import os
import pickle
import re
import sys
import tempfile
import threading
import unicodedata
from collections import Counter, OrderedDict
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

from bs4 import BeautifulSoup, Comment
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from core.config import settings
from core.known_words import load_local_vocabulary
from core.paths import get_book_path, get_video_path
from integrations.anki import get_anki_word_sets

logger = logging.getLogger(__name__)
VOCABULARY_STATS_VERSION = 1
WINDOW_SIZE = 1000
BASE_DICTIONARY = Path(__file__).resolve().parent.parent / "static" / "dict.json"
VOCABULARY_STATS_FILENAME = "coverage.json"
MAX_WORD_LEN = 8
OOV_PENALTY = math.log(50_001)
RANK_EPSILON = 1e-10
NUMBERS = set("零一二三四五六七八九十百千万亿两")
BU_IDIOMS = {"不得了", "不要紧", "不得不", "不由得"}
CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
BLOCKS = set("p div h1 h2 h3 h4 h5 h6 li blockquote pre td th".split())
EXCLUDED = {"rt", "rp", "script", "style"}
_vocabulary_stats_locks = WeakValueDictionary()
_vocabulary_stats_locks_lock = threading.Lock()


@lru_cache(maxsize=8192)
def is_punct(word: str) -> bool:
    return bool(word) and (
        all(
            c.isspace() or unicodedata.category(c)[0] in "PZC" or c in "～×÷"
            for c in word
        )
        or all(c in "0123456789０１２３４５６７８９" for c in word)
    )


def compositionally_known(word: str, known: set[str]) -> bool:
    if word and all(c in NUMBERS for c in word):
        return True
    if len(word) >= 2 and word.endswith("们"):
        return word[:-1] in known
    if len(word) >= 2 and word.startswith("第"):
        return all(c in NUMBERS for c in word[1:])
    if len(word) >= 2 and word.startswith("不") and word not in BU_IDIOMS:
        return word[1:] in known
    return False


def segment(sentence: str, dictionary: dict) -> list[str]:
    # JS indexes UTF-16 code units, including when a sentence contains emoji.
    astral = any(ord(c) > 0xFFFF for c in sentence)
    if astral:
        encoded = sentence.encode("utf-16-le", errors="surrogatepass")
        sentence = "".join(
            chr(int.from_bytes(encoded[i : i + 2], "little"))
            for i in range(0, len(encoded), 2)
        )
    n = len(sentence)
    scores = [None] * (n + 1)
    previous = [None] * (n + 1)
    scores[0] = (0, 0.0, 0)
    for i in range(1, n + 1):
        for j in range(max(0, i - MAX_WORD_LEN), i):
            word = sentence[j:i]
            if astral:
                word = word.encode("utf-16-le", errors="surrogatepass").decode(
                    "utf-16-le", errors="surrogatepass"
                )
            if is_punct(word):
                oov, rank = 0, 0
            elif word in dictionary:
                frequency = dictionary[word].get("f")
                frequency = 8000 if frequency is None else frequency
                oov, rank = 0, math.log(frequency + 1) - 4 * (i - j - 1)
            else:
                oov, rank = i - j, OOV_PENALTY * (i - j)
            prior = scores[j]
            candidate = (prior[0] + oov, prior[1] + rank, prior[2] + 1)
            best = scores[i]
            if (
                best is None
                or candidate[0] < best[0]
                or (candidate[0] == best[0] and candidate[1] < best[1] - RANK_EPSILON)
                or (
                    candidate[0] == best[0]
                    and abs(candidate[1] - best[1]) <= RANK_EPSILON
                    and candidate[2] < best[2]
                )
            ):
                scores[i] = candidate
                previous[i] = (j, word)
    words = []
    while n:
        n, word = previous[n]
        words.append(word)
    return words[::-1]


def html_blocks(html: str) -> list[str]:
    """Join inline nodes, preserve reading blocks, and exclude ruby annotations."""
    soup = BeautifulSoup(html, "html.parser")
    groups = []
    previous_block = None
    for node in soup.find_all(string=True):
        if isinstance(node, Comment) or any(p.name in EXCLUDED for p in node.parents):
            continue
        block = next((p for p in node.parents if p.name in BLOCKS), soup)
        if block is not previous_block:
            groups.append([])
            previous_block = block
        groups[-1].append(str(node))
    return ["".join(group) for group in groups]


def subtitle_sections(text: str):
    for block in re.split(
        r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip()
    ):
        lines = block.splitlines()
        if len(lines) < 3 or " --> " not in lines[1]:
            continue
        label = lines[1].split(" --> ")[0]
        yield label, [re.sub(r"<[^>]+>", "", " ".join(lines[2:]))]


def build_vocabulary_stats(sections, dictionary: dict, window_size: int = WINDOW_SIZE) -> dict:
    counts, current = Counter(), Counter()
    windows, markers, oov = [], [], set()
    total = 0
    for label, blocks in sections:
        start = total
        for block in blocks:
            if not CHINESE_RE.search(block):
                continue
            for sentence in re.split(r"(?<=[。！？…])", block):
                for word in segment(sentence, dictionary):
                    if is_punct(word) or not CHINESE_RE.search(word):
                        continue
                    counts[word] += 1
                    current[word] += 1
                    total += 1
                    if word not in dictionary:
                        oov.add(word)
                    if total % window_size == 0:
                        windows.append(dict(current))
                        current.clear()
        if total > start:
            markers.append({"label": label, "start": start, "end": total})
    if current:
        windows.append(dict(current))
    return {
        "counts": dict(counts),
        "windows": windows,
        "markers": markers,
        "oov": sorted(oov),
        "total": total,
        "window_size": window_size,
    }


def score_vocabulary_stats(vocabulary_stats: dict, known: set[str], *, detail: bool = False) -> dict:
    oov = set(vocabulary_stats["oov"])
    understood = {
        word
        for word in vocabulary_stats["counts"]
        if word in known or (word not in oov and compositionally_known(word, known))
    }

    def score(counts):
        total = sum(counts.values())
        unfamiliar = sum(
            count for word, count in counts.items() if word not in understood
        )
        return {
            "total": total,
            "unfamiliar": unfamiliar,
            "coverage": 100 * (total - unfamiliar) / total if total else None,
            "unfamiliar_per_1000": 1000 * unfamiliar / total if total else None,
            "unknown_words": sum(word not in understood for word in counts),
            "unrecognized": sum(count for word, count in counts.items() if word in oov),
        }

    result = {
        "status": "ready" if vocabulary_stats["total"] else "empty",
        **score(vocabulary_stats["counts"]),
    }
    if detail:
        result["windows"] = []
        start = 0
        for counts in vocabulary_stats["windows"]:
            entry = score(counts)
            entry.update(start=start, end=start + entry["total"])
            entry["words"] = sorted(
                (
                    {"word": word, "count": count, "unrecognized": word in oov}
                    for word, count in counts.items()
                    if word not in understood
                ),
                key=lambda item: (-item["count"], item["word"]),
            )
            result["windows"].append(entry)
            start = entry["end"]
        result["markers"] = vocabulary_stats["markers"]
        result["window_size"] = vocabulary_stats["window_size"]
    return result


def item_paths(kind: str, item_id: str):
    if kind not in {"books", "videos"}:
        raise ValueError("Unknown coverage kind")
    folder = get_book_path(item_id) if kind == "books" else get_video_path(item_id)
    source = folder / ("book.pkl" if kind == "books" else "subtitles.srt")
    generated = folder / (
        "book_dict.json" if kind == "books" else "subtitles_dict.json"
    )
    return folder, source, generated


def file_signature(path: Path):
    try:
        stat = path.stat()
        return [stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size]
    except FileNotFoundError:
        return None


@lru_cache(maxsize=1)
def _load_base_dictionary(signature: tuple):
    return json.loads(BASE_DICTIONARY.read_text(encoding="utf-8"))


def _input_signature(source: Path, generated: Path) -> list:
    return [
        VOCABULARY_STATS_VERSION,
        WINDOW_SIZE,
        settings.dictionary_generation.enabled,
        file_signature(source),
        file_signature(BASE_DICTIONARY),
        file_signature(generated),
    ]


def _read_cached_vocabulary_stats(folder: Path, signature: list) -> dict | None:
    try:
        vocabulary_stats = json.loads(
            (folder / VOCABULARY_STATS_FILENAME).read_text(encoding="utf-8")
        )
        if vocabulary_stats.get("signature") == signature and all(
            key in vocabulary_stats
            for key in ("counts", "windows", "markers", "oov", "total", "window_size")
        ):
            return vocabulary_stats
    except (FileNotFoundError, ValueError, AttributeError):
        pass
    return None


def _vocabulary_stats_lock_for(folder: Path):
    # Weak references retire locks once no builder or waiter uses them.
    with _vocabulary_stats_locks_lock:
        lock = _vocabulary_stats_locks.get(folder)
        if lock is None:
            lock = threading.Lock()
            _vocabulary_stats_locks[folder] = lock
        return lock


def ensure_vocabulary_stats(
    kind: str, item_id: str, *, build: bool = True
) -> tuple[dict | None, str]:
    """Read cached vocabulary statistics or build them under the item's lock."""
    folder, source, generated = item_paths(kind, item_id)
    signature = _input_signature(source, generated)
    if signature[3] is not None:
        vocabulary_stats = _read_cached_vocabulary_stats(folder, signature)
        if vocabulary_stats is not None:
            return vocabulary_stats, "ready"
    if not build:
        return _build_or_load_vocabulary_stats(kind, item_id, build=False)
    with _vocabulary_stats_lock_for(folder):
        # Recheck after acquiring the lock: another caller may have built it.
        return _build_or_load_vocabulary_stats(kind, item_id)


def _build_or_load_vocabulary_stats(
    kind: str, item_id: str, *, build: bool = True
) -> tuple[dict | None, str]:
    """Inspect inputs; building requires the item's lock and stable inputs."""
    folder, source, generated = item_paths(kind, item_id)

    before = _input_signature(source, generated)
    if before[3] is None:
        return None, "no_subtitles" if kind == "videos" else "missing"
    try:
        state = json.loads(generated.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    if settings.dictionary_generation.enabled and state.get("status") != "done":
        status = "dictionary_error" if state.get("status") == "error" else "pending"
        return None, status
    vocabulary_stats = _read_cached_vocabulary_stats(folder, before)
    if vocabulary_stats is not None:
        return vocabulary_stats, "ready"
    if not build:
        return None, "analysing"
    destination = folder / VOCABULARY_STATS_FILENAME
    dictionary = dict(_load_base_dictionary(tuple(before[4])))
    # Match dictionary.js: generated entries do not replace base entries.
    for word in state.get("words", {}):
        dictionary.setdefault(word, {"f": 100})
    if kind == "books":
        with source.open("rb") as file:
            book = pickle.load(file)
        sections = (
            (chapter.title, html_blocks(chapter.content)) for chapter in book.spine
        )
    else:
        sections = subtitle_sections(source.read_text(encoding="utf-8-sig"))
    vocabulary_stats = build_vocabulary_stats(sections, dictionary)
    vocabulary_stats["signature"] = before
    if _input_signature(source, generated) != before:
        return None, "pending"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=folder,
            prefix=".coverage-",
            delete=False,
        ) as file:
            temporary = Path(file.name)
            json.dump(
                vocabulary_stats, file, ensure_ascii=False, separators=(",", ":")
            )
        if _input_signature(source, generated) != before:
            return None, "pending"
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return vocabulary_stats, "ready"


_coordinator = None


def _analysis_status(kind, item_id):
    return ensure_vocabulary_stats(kind, item_id, build=False)[1]


def _stale_items():
    roots = [("books", settings.paths.library, "book.pkl")]
    if settings.video.enabled:
        roots.append(("videos", settings.paths.video_library, "video.pkl"))
    for kind, root, metadata in roots:
        if not root.is_dir():
            continue
        for folder in root.iterdir():
            try:
                item_id = str(UUID(folder.name))
            except ValueError:
                continue
            if not (folder / metadata).is_file():
                continue
            try:
                if _analysis_status(kind, item_id) == "analysing":
                    yield kind, item_id
            except Exception:
                logger.exception("Could not inspect vocabulary for %s %s", kind, item_id)


class CoverageJobs:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.pending = set()
        self.running = None
        self.requested_again = False
        self.process = None

    def enqueue(self, kind, item_id):
        key = (kind, str(UUID(str(item_id))))
        if key == self.running:
            # A dictionary completion may race with the worker's final check.
            self.requested_again = True
        if key not in self.pending:
            self.pending.add(key)
            self.queue.put_nowait(key)

    async def scan(self):
        items = _stale_items()
        while (key := await asyncio.to_thread(next, items, None)) is not None:
            self.enqueue(*key)

    async def _analyse(self, key):
        if self.process is None or self.process.returncode is not None:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "core.coverage",
                cwd=Path(__file__).resolve().parent.parent,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )
        self.process.stdin.write((json.dumps(key) + "\n").encode())
        await self.process.stdin.drain()
        line = await self.process.stdout.readline()
        if not line:
            await self.process.wait()
            raise RuntimeError("Vocabulary analysis worker exited unexpectedly")
        result = json.loads(line)
        if "error" in result:
            raise RuntimeError(result["error"])

    async def run(self):
        while True:
            key = await self.queue.get()
            self.running = key
            self.requested_again = False
            retry = False
            try:
                if await asyncio.to_thread(_analysis_status, *key) == "analysing":
                    await self._analyse(key)
                    # Changed inputs are deliberately not saved by the builder.
                    retry = await asyncio.to_thread(_analysis_status, *key) == "analysing"
            except Exception:
                logger.exception("Could not analyse vocabulary for %s %s", *key)
            finally:
                self.pending.discard(key)
                self.running = None
                if retry or self.requested_again:
                    self.enqueue(*key)
                self.queue.task_done()

    async def stop(self):
        if self.process is None:
            return
        if self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()


async def refresh_coverage(kind: str, item_id: str) -> None:
    # During shutdown, work can safely wait for the next startup scan.
    if _coordinator is not None:
        _coordinator.enqueue(kind, item_id)


@asynccontextmanager
async def manage_coverage_jobs():
    global _coordinator
    coordinator = CoverageJobs()
    _coordinator = coordinator
    runner = asyncio.create_task(coordinator.run())
    scan = asyncio.create_task(coordinator.scan())
    try:
        yield
    finally:
        _coordinator = None
        scan.cancel()
        runner.cancel()
        await asyncio.gather(scan, runner, return_exceptions=True)
        await coordinator.stop()


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


def _score(kind: str, item_id: str, known: set[str], *, detail=False):
    try:
        vocabulary_stats, status = ensure_vocabulary_stats(kind, item_id, build=False)
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
                result[item_id] = _score(kind, item_id, known)
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
    snapshot: UUID | None = None,
    refresh: bool = False,
):
    result = await asyncio.to_thread(_library_scores, kind, snapshot, refresh)
    for item_id, score in result["items"].items():
        if score["status"] == "analysing":
            await refresh_coverage(kind, item_id)
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

    result = await asyncio.to_thread(load)
    if result["status"] == "analysing":
        await refresh_coverage(kind, str(item_id))
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


def main():
    for line in sys.stdin:
        try:
            kind, item_id = json.loads(line)
            _, status = ensure_vocabulary_stats(kind, item_id)
            result = {"status": status}
        except Exception as error:
            result = {"error": f"{type(error).__name__}: {error}"}
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
