"""Pull-driven PCM with bounded live history and lossless chunk replay."""

import collections
import contextlib
import gzip
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
from weakref import WeakValueDictionary

from fastapi import HTTPException
from core.paths import named_temp_path

SAMPLE_RATE = 48_000
CHANNELS = 2
FRAME_BYTES = CHANNELS * 2
CHUNK_SECONDS = 8
MAX_STREAMS = 4
HISTORY_CHUNKS = 6
IDLE_SECONDS = 90
_lock = threading.RLock()
_streams = {}
_session_locks = WeakValueDictionary()
_generations = {}


REPLAY_CACHE_BYTES = 128 * 1024**2
REPLAY_MAX_FILES = 2048
REPLAY_MAX_CHUNK_BYTES = 8 * 48000 * 4 * 2  # Eight source seconds at minimum speed 0.5x.
_replay_lock = threading.Lock()


def _replay_root():
    root = named_temp_path("playback-audio-chunks-v1")
    root.mkdir(mode=0o700, exist_ok=True)
    return root


def _replay_path(identity, chunk):
    source, version, _generation, origin, rate = identity
    # Generation/session do not alter samples; source version, tempo and filter
    # origin do. Never splice cached chunks from different tempo-filter runs.
    key = json.dumps([source, version, origin, format(rate, "g"), chunk]).encode()
    return _replay_root() / (hashlib.sha256(key).hexdigest() + ".gz")


def _read_replay(identity, chunk):
    path = _replay_path(identity, chunk)
    with _replay_lock:
        try:
            file = path.open("rb")
        except FileNotFoundError:
            return None
        os.utime(file.fileno(), None)
    try:
        with file, gzip.GzipFile(fileobj=file) as compressed:
            data = compressed.read(REPLAY_MAX_CHUNK_BYTES + 1)
        if len(data) > REPLAY_MAX_CHUNK_BYTES:
            raise ValueError("Oversized cached chunk")
        return data
    except (OSError, EOFError, ValueError):
        with _replay_lock:
            path.unlink(missing_ok=True)
        return None


def _write_replay(identity, chunk, data):
    if len(data) > REPLAY_MAX_CHUNK_BYTES:
        raise ValueError("Oversized PCM chunk")
    path = _replay_path(identity, chunk)
    encoded = gzip.compress(data, compresslevel=1, mtime=0)
    with _replay_lock:
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
            if total <= REPLAY_CACHE_BYTES and count <= REPLAY_MAX_FILES:
                break
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
            except PermissionError:
                continue  # An open response may temporarily pin a file on Windows.
            total -= stat.st_size
            count -= 1


def expired_window():
    return HTTPException(410, {"code": "AUDIO_WINDOW_EXPIRED", "message": "Audio window expired; restart near the requested position."})


class AudioStream:
    def __init__(self, source, version, generation, origin, rate, duration):
        self.identity = (str(source), version, generation, origin, rate)
        self.generation = generation
        self.origin = origin
        self.rate = rate
        self.duration = duration
        self.next = origin
        self.history = collections.OrderedDict()
        self.lock = threading.Lock()
        self.used = time.monotonic()
        self.process = subprocess.Popen([
            "ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-filter_threads", "1",
            "-ss", str(origin * CHUNK_SECONDS), "-i", str(source),
            "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", str(CHANNELS),
            "-af", f"aresample={SAMPLE_RATE}:async=1:first_pts=0,atempo={rate},apad",
            "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1",
        ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        self.process.stdout.close()
        self.history.clear()

    def read(self, chunk):
        if chunk in self.history:
            return self.history[chunk]
        # Forward chunks must still advance the continuous decoder. Replaying
        # older chunks, however, must not rewind or replace that live process.
        if chunk < self.next:
            cached = _read_replay(self.identity, chunk)
            if cached is not None:
                return cached
        if chunk < self.next or chunk > self.next + 3:
            raise expired_window()
        # The pipe provides backpressure: FFmpeg cannot decode the rest of a
        # movie while the client is paused or has enough audio buffered.
        def timeout():
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
        timer = threading.Timer(30, timeout)
        timer.daemon = True
        timer.start()
        try:
            while self.next <= chunk:
                first = round((self.next - self.origin) * CHUNK_SECONDS * SAMPLE_RATE / self.rate)
                end = min((self.next + 1) * CHUNK_SECONDS, self.duration)
                last = round((end - self.origin * CHUNK_SECONDS) * SAMPLE_RATE / self.rate)
                required = (last - first) * FRAME_BYTES
                data = bytearray()
                while len(data) < required:
                    part = self.process.stdout.read(min(65536, required - len(data)))
                    if not part:
                        raise HTTPException(503, "Could not decode audio. Please retry.")
                    data.extend(part)
                self.history[self.next] = bytes(data)
                _write_replay(self.identity, self.next, self.history[self.next])
                self.next += 1
                while len(self.history) > HISTORY_CHUNKS:
                    self.history.popitem(last=False)
        finally:
            timer.cancel()
            timer.join()
        return self.history[chunk]


def session_lock(session):
    with _lock:
        lock = _session_locks.get(session)
        if lock is None:
            lock = threading.Lock()
            _session_locks[session] = lock
        return lock


def read_chunk(session, source, version, generation, origin, rate, duration, chunk):
    with session_lock(session):
        return _read_chunk(session, source, version, generation, origin, rate, duration, chunk)


def _read_chunk(session, source, version, generation, origin, rate, duration, chunk):
    identity = (str(source), version, generation, origin, rate)
    key = (session, rate)
    with _lock:
        owned = [(k, stream) for k, stream in _streams.items() if k[0] == session]
        previous = _generations.get(session)
        if previous and generation < previous[0]:
            raise HTTPException(409, {"code": "AUDIO_SUPERSEDED", "message": "Audio request was superseded."})
        _generations[session] = (generation, time.monotonic())
        # One seek generation can retain two continuous rates (normal and 2x).
        # A new generation replaces both; unrelated viewers remain untouched.
        for old_key, old in owned:
            if old.identity[:4] != identity[:4]:
                old.close()
                del _streams[old_key]
        stream = _streams.get(key)
        if stream is not None:
            stream.used = time.monotonic()
    # Reuse disk chunks even after the decoder has expired. No process slot or
    # replay from the beginning of a long file is needed for a cache hit.
    if stream is None:
        cached = _read_replay(identity, chunk)
        if cached is not None and chunk > origin + 3:
            return cached
        if chunk > origin + 3:
            raise expired_window()
    with _lock:
        stream = _streams.get(key)
        if stream is None:
            owned = [(k, s) for k, s in _streams.items() if k[0] == session]
            if len(owned) >= 2:
                old_key, old = min(owned, key=lambda pair: pair[1].used)
                old.close()
                del _streams[old_key]
            if len(_streams) >= MAX_STREAMS:
                # Paused tabs and speculative rates retain backpressured
                # decoders. Reclaim the least recently used pipe that is not
                # being read, rather than making a new viewer wait for expiry.
                # Its PCM remains in the replay cache; a later cache miss uses
                # the existing AUDIO_WINDOW_EXPIRED recovery near the playhead.
                for old_key, old in sorted(_streams.items(), key=lambda pair: pair[1].used):
                    if not old.lock.acquire(blocking=False):
                        continue
                    try:
                        old.close()
                        del _streams[old_key]
                    finally:
                        old.lock.release()
                    break
                else:
                    raise HTTPException(503, "All audio decoders are busy. Please retry.")
            stream = AudioStream(source, version, generation, origin, rate, duration)
            _streams[key] = stream
        stream.used = time.monotonic()
        # Same-session reads are serialized by session_lock. Reserve the pipe
        # under the pool lock so the idle reaper cannot close it during a read.
        stream.lock.acquire()
    try:
        result = stream.read(chunk)
        stream.used = time.monotonic()
        return result
    except Exception as error:
        # Missing speculative history must not destroy upcoming live audio.
        if isinstance(error, HTTPException) and error.status_code == 410:
            raise
        with _lock:
            if _streams.get(key) is stream:
                del _streams[key]
        stream.close()
        raise
    finally:
        stream.lock.release()


def expire_streams(all_streams=False):
    with _lock:
        for key, stream in list(_streams.items()):
            if (all_streams or time.monotonic() - stream.used > IDLE_SECONDS) and stream.lock.acquire(blocking=False):
                try:
                    stream.close()
                    del _streams[key]
                finally:
                    stream.lock.release()
        for session, (_, used) in list(_generations.items()):
            if all_streams or (time.monotonic() - used > IDLE_SECONDS and not any(k[0] == session for k in _streams)):
                del _generations[session]


def release_session(session, generation=None):
    with session_lock(session):
        with _lock:
            streams = [(key, stream) for key, stream in _streams.items()
                       if key[0] == session and (generation is None or generation >= stream.generation)]
            for key, _ in streams:
                del _streams[key]
        for _, stream in streams:
            stream.close()


@contextlib.contextmanager
def manage_audio_streams():
    stop = threading.Event()
    def reap():
        while not stop.wait(10):
            expire_streams()
    thread = threading.Thread(target=reap, daemon=True, name="playback-audio-cleanup")
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
        expire_streams(all_streams=True)
