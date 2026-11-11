"""Persistent vocabulary statistics for texts and live learner coverage.

Segmentation mirrors static/segmenter.js with an empty known-word set. Keep the
parity test when changing either implementation; bump VOCABULARY_STATS_VERSION
if word boundaries, extraction, or classification rules change.
"""

import asyncio
import gzip
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
from collections import Counter
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

from bs4 import BeautifulSoup, Comment
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from core import known_words
from core.config import settings
from core.paths import get_book_path, get_video_path
from integrations import anki

logger = logging.getLogger(__name__)
DETAIL_FILENAME = "coverage-result.json.gz"
_lock = threading.RLock()
_state_path = None
_state = None
_dirty = threading.Event()
_sync = threading.Event()
_library_sync = threading.Event()
_syncing = False
_running = False
_anki_revision = 0
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


def build_vocabulary_stats(
    sections, dictionary: dict, window_size: int = WINDOW_SIZE
) -> dict:
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


def score_vocabulary_stats(vocabulary_stats: dict, known: set[str]) -> dict:
    """Score every section directly into the shared word-table format."""
    oov = set(vocabulary_stats["oov"])
    counts = vocabulary_stats["counts"]
    unknown = sorted(
        (
            word
            for word in counts
            if word not in known
            and (word in oov or not compositionally_known(word, known))
        ),
        key=lambda word: (-counts[word], word),
    )
    ids = {word: index for index, word in enumerate(unknown)}

    def score(counts):
        words = sorted(
            (word for word in counts if word in ids),
            key=lambda word: (-counts[word], word),
        )
        total = sum(counts.values())
        unfamiliar = sum(counts[word] for word in words)
        return {
            "total": total,
            "unfamiliar": unfamiliar,
            "coverage": 100 * (total - unfamiliar) / total if total else None,
            "unknown_words": len(words),
            "words": [value for word in words for value in (ids[word], counts[word])],
        }

    result = {
        "status": "ready" if vocabulary_stats["total"] else "empty",
        **score(counts),
        "vocabulary": [[word, int(word in oov)] for word in unknown],
        "windows": [],
        "markers": vocabulary_stats["markers"],
        "window_size": vocabulary_stats["window_size"],
    }
    start = 0
    for counts in vocabulary_stats["windows"]:
        entry = score(counts)
        entry.update(start=start, end=start + entry["total"])
        result["windows"].append(entry)
        start = entry["end"]
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
            json.dump(vocabulary_stats, file, ensure_ascii=False, separators=(",", ":"))
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
                logger.exception(
                    "Could not inspect vocabulary for %s %s", kind, item_id
                )


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
                sys.executable,
                "-m",
                "core.coverage",
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
                    retry = (
                        await asyncio.to_thread(_analysis_status, *key) == "analysing"
                    )
            except Exception:
                logger.exception("Could not analyse vocabulary for %s %s", *key)
            else:
                vocabulary_changed()
            finally:
                # Failures wait for a fresh rescan instead of retriggering themselves.
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


def _load():
    global _state, _state_path
    path = settings.paths.library / "coverage-state.json"
    if _state_path != path or _state is None:
        _state_path = path
        try:
            _state = json.loads(path.read_text(encoding="utf-8"))
            if _state.get("version") != 1:
                raise ValueError("Old coverage cache")
        except (OSError, ValueError, AttributeError):
            _state = {"version": 1, "anki": None, "items": {}}
    return _state


def _write(path, payload):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".coverage-", delete=False
        ) as file:
            temporary = Path(file.name)
            file.write(payload)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def saved_detail(kind, item_id, revision):
    """Read the payload and its revision together while the worker publishes it."""

    folder, _, _ = item_paths(kind, item_id)
    with _lock:
        entry = _load()["items"].get(f"{kind}/{item_id}")
        try:
            payload = (folder / DETAIL_FILENAME).read_bytes() if entry else None
        except FileNotFoundError:
            payload = None
        if payload is None:
            vocabulary_changed()
            return None, False
        return payload, revision is not None and revision == entry["summary"][
            "revision"
        ]


def vocabulary_changed():
    _dirty.set()


def request_anki_refresh():
    _sync.set()


def request_library_sync():
    """Rescan content on entry and share an in-flight sync without waiting."""
    if settings.anki.enabled:
        # Content can change even when Anki returns the same known-word set.
        vocabulary_changed()
        with _lock:
            if not _syncing:
                _library_sync.set()


def anki_revision():
    with _lock:
        return _anki_revision


def remember_anki_words(words, *, expected_revision=None):
    global _anki_revision
    with _lock:
        if expected_revision is not None and expected_revision != _anki_revision:
            request_anki_refresh()
            return
        state = _load()
        words = sorted(set(words))
        if state["anki"] != words:
            _anki_revision += 1
            state["anki"] = words
            vocabulary_changed()


def remember_created_word(word):
    with _lock:
        words = _load()["anki"]
        if words is None:
            request_anki_refresh()
        else:
            remember_anki_words(set(words) | {word})


def saved_library(kind):
    with _lock:
        return {
            key.split("/", 1)[1]: entry["summary"]
            for key, entry in _load()["items"].items()
            if key.startswith(kind + "/")
        }


def library_result(kind):
    local = known_words.load_local_vocabulary()
    return {
        "items": saved_library(kind),
        "local_words": local["words"],
        "local_revision": local["revision"],
        "refreshing": _dirty.is_set() or _running,
    }


def _dependencies(stats):

    words = set(stats["counts"])
    oov = set(stats["oov"])
    for word in stats["counts"]:
        if word in oov or len(word) < 2:
            continue
        if word.endswith("们"):
            words.add(word[:-1])
        elif word.startswith("不") and word not in BU_IDIOMS:
            words.add(word[1:])
    return sorted(words)


def update_results() -> list[tuple[str, str]]:
    """Update affected scores and return items that need vocabulary analysis."""

    local = known_words.load_local_vocabulary()
    with _lock:
        state = _load()
        if state["anki"] is None:
            return []
        known = set(state["anki"]) | set(local["words"])
        previous = dict(state["items"])
    entries = {}
    missing_analysis = []
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
            key = f"{kind}/{item_id}"
            old = previous.get(key, {})
            try:
                _, source, generated = item_paths(kind, item_id)
                signature = _input_signature(source, generated)
                relevant = sorted(known.intersection(old.get("dependencies", [])))
                if (
                    old.get("signature") == signature
                    and old.get("known") == relevant
                    and (folder / DETAIL_FILENAME).is_file()
                ):
                    entries[key] = old
                    continue
                stats, status = ensure_vocabulary_stats(kind, item_id, build=False)
                if status == "analysing":
                    missing_analysis.append((kind, item_id))
                detail = (
                    score_vocabulary_stats(stats, known)
                    if stats
                    else {"status": status}
                )
                revision = str(uuid4())
                detail["revision"] = revision
                dependencies = _dependencies(stats) if stats else []
                summary = {
                    k: v
                    for k, v in detail.items()
                    if k
                    not in {"vocabulary", "words", "windows", "markers", "window_size"}
                }
                entry = {
                    "summary": summary,
                    "dependencies": dependencies,
                    "known": sorted(known.intersection(dependencies)),
                    # Pending analysis must be revisited when its worker finishes.
                    "signature": signature if stats else None,
                }
                if _input_signature(source, generated) != signature:
                    vocabulary_changed()
                    if old:
                        entries[key] = old
                    continue
                payload = gzip.compress(
                    json.dumps(
                        detail,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode(),
                    compresslevel=6,
                    mtime=0,
                )
                with _lock:
                    _write(folder / DETAIL_FILENAME, payload)
                    state["items"][key] = entry
                entries[key] = entry
            except Exception:
                logger.exception("Could not update saved coverage for %s", key)
                if old:
                    entries[key] = {**old, "signature": None}
    with _lock:
        state["items"] = entries
        _state_path.parent.mkdir(parents=True, exist_ok=True)
        _write(
            _state_path,
            json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode(),
        )

    return missing_analysis


async def run_updates():

    global _running
    vocabulary_changed()
    while True:
        await asyncio.sleep(0.2)
        if not _dirty.is_set() or not settings.anki.enabled:
            continue
        _running = True
        _dirty.clear()
        work = asyncio.create_task(asyncio.to_thread(update_results))
        try:
            missing_analysis = await asyncio.shield(work)
            # The queue belongs to this event loop, not the scoring thread.
            for kind, item_id in missing_analysis:
                await refresh_coverage(kind, item_id)
        except asyncio.CancelledError:
            # A thread cannot be cancelled: finish its atomic saves before shutdown.
            await work
            raise
        except Exception:
            logger.exception("Could not save vocabulary coverage")
        finally:
            _running = False


async def refresh_anki():
    global _syncing
    request_anki_refresh()
    while True:
        if settings.anki.enabled and (_sync.is_set() or _library_sync.is_set()):
            with _lock:
                _syncing = _library_sync.is_set()
                _library_sync.clear()
                _sync.clear()
            try:
                if _syncing:
                    await anki.call_anki_async("sync")
                    # Sync completion requests a vocabulary read; this job supplies it.
                    _sync.clear()
                await asyncio.to_thread(anki.get_anki_word_sets, sync=False)
            except Exception:
                logger.exception("Could not refresh vocabulary; keeping saved coverage")
            finally:
                with _lock:
                    _syncing = False
        await asyncio.sleep(1)


@asynccontextmanager
async def manage_coverage_jobs():
    global _coordinator

    updates = asyncio.create_task(run_updates())
    vocabulary = asyncio.create_task(refresh_anki())
    coordinator = CoverageJobs()
    _coordinator = coordinator
    runner = asyncio.create_task(coordinator.run())
    scan = asyncio.create_task(coordinator.scan())
    try:
        yield
    finally:
        _coordinator = None
        updates.cancel()
        vocabulary.cancel()
        scan.cancel()
        runner.cancel()
        await asyncio.gather(scan, runner, updates, vocabulary, return_exceptions=True)
        await coordinator.stop()


router = APIRouter(prefix="/api/coverage")
Kind = Literal["books", "videos"]


def _check_enabled(kind):
    if kind == "videos" and not settings.video.enabled:
        raise HTTPException(404, "Videos are disabled")
    if not settings.anki.enabled:
        raise HTTPException(409, "Enable Anki to see your vocabulary coverage.")


@router.get("/{kind}")
def library_coverage(kind: Kind):

    _check_enabled(kind)
    return JSONResponse(library_result(kind), headers={"Cache-Control": "no-store"})


@router.get("/{kind}/{item_id}")
def item_coverage(
    kind: Kind, item_id: UUID, request: Request, revision: UUID | None = None
):

    _check_enabled(kind)
    folder, _, _ = item_paths(kind, str(item_id))
    if not folder.is_dir():
        raise HTTPException(404, "Item not found")
    payload, current = saved_detail(
        kind, str(item_id), str(revision) if revision else None
    )
    if payload is None:
        return JSONResponse(
            {"status": "analysing"}, headers={"Cache-Control": "no-store"}
        )
    headers = {
        "Cache-Control": "private, max-age=31536000, immutable"
        if current
        else "no-store",
        "Vary": "Accept-Encoding",
    }
    accepted = re.search(
        r"(?:^|,)\s*gzip\s*(?:;\s*q=(0(?:\.\d+)?|1(?:\.0+)?))?\s*(?:,|$)",
        request.headers.get("accept-encoding", ""),
        re.IGNORECASE,
    )
    if accepted and float(accepted.group(1) or "1") > 0:
        headers["Content-Encoding"] = "gzip"
    else:
        payload = gzip.decompress(payload)
    return Response(payload, media_type="application/json", headers=headers)


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
