"""Bounded, lossless replay cache for already generated audio chunks."""

import gzip
import hashlib
import json
import os
import tempfile
import threading

from core.paths import named_temp_path

CACHE_BYTES = 128 * 1024**2
MAX_FILES = 2048
MAX_CHUNK_BYTES = 8 * 48000 * 4 * 2  # Eight source seconds at minimum speed 0.5x.
_lock = threading.Lock()


def cache_root():
    root = named_temp_path("playback-audio-chunks-v1")
    root.mkdir(mode=0o700, exist_ok=True)
    return root


def _path(identity, chunk):
    source, version, _generation, origin, rate = identity
    # Generation/session do not alter samples; source version, tempo and filter
    # origin do. Never splice cached chunks from different tempo-filter runs.
    key = json.dumps([source, version, origin, format(rate, "g"), chunk]).encode()
    return cache_root() / (hashlib.sha256(key).hexdigest() + ".gz")


def read(identity, chunk):
    path = _path(identity, chunk)
    with _lock:
        try:
            file = path.open("rb")
        except FileNotFoundError:
            return None
        os.utime(file.fileno(), None)
    try:
        with file, gzip.GzipFile(fileobj=file) as compressed:
            data = compressed.read(MAX_CHUNK_BYTES + 1)
        if len(data) > MAX_CHUNK_BYTES:
            raise ValueError("Oversized cached chunk")
        return data
    except (OSError, EOFError, ValueError):
        with _lock:
            path.unlink(missing_ok=True)
        return None


def write(identity, chunk, data):
    if len(data) > MAX_CHUNK_BYTES:
        raise ValueError("Oversized PCM chunk")
    path = _path(identity, chunk)
    encoded = gzip.compress(data, compresslevel=1, mtime=0)
    with _lock:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as file:
            temporary = path.parent / os.path.basename(file.name)
            file.write(encoded)
        try:
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        entries = []
        for candidate in path.parent.glob("*.gz"):
            try:
                entries.append((candidate, candidate.stat()))
            except FileNotFoundError:
                pass
        total = sum(stat.st_size for _, stat in entries)
        count = len(entries)
        for candidate, stat in sorted(entries, key=lambda item: item[1].st_mtime):
            if total <= CACHE_BYTES and count <= MAX_FILES:
                break
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
            except PermissionError:
                continue  # An open response may temporarily pin a file on Windows.
            total -= stat.st_size
            count -= 1
