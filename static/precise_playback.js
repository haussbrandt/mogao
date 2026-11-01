/* Video-only Media Source playback with bounded, separately decoded audio. */
(() => {
  "use strict";
  const VIDEO_BYTES = 64 * 1024 * 1024; // Cache target and hard per-fragment limit.
  const JUMP_SECONDS = 10;
  const BUFFER_BEHIND = 20;
  const BUFFER_AHEAD = 40;
  const AUDIO_CHANNELS = 2;
  const FLOAT_BYTES = 4;
  const AUDIO_BYTES = 48 * 1024 * 1024;
  function timeout(promise, description, ms = 30000) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(description)), ms);
      Promise.resolve(promise).then(value => { clearTimeout(timer); resolve(value); },
        error => { clearTimeout(timer); reject(error); });
    });
  }
  function event(target, name, action) {
    return new Promise((resolve, reject) => {
      const clean = () => { clearTimeout(timer); target.removeEventListener(name, done); target.removeEventListener("error", fail); };
      const done = () => { clean(); resolve(); };
      const fail = () => { clean(); reject(new Error("The browser could not decode this video.")); };
      const timer = setTimeout(() => { clean(); reject(new Error("Playback took too long to prepare.")); }, 30000);
      target.addEventListener(name, done, { once: true });
      target.addEventListener("error", fail, { once: true });
      try { action(); } catch (error) { clean(); reject(error); }
    });
  }

  class PrecisePlayback {
    constructor(video, config) {
      this.video = video;
      this.base = `${config.videoBasePath}/stream/precise/${config.videoId}`;
      this.pendingTime = Math.max(0, Number(config.initialPlaybackTime) || 0);
      this.seekPending = true;
      this.rate = 1;
      this.normalRate = 1;
      this.wanted = false;
      this.revision = 0;
      this.lifetime = new AbortController();
      this.audio = new Map();
      this.segmentRequests = new Map();
      this.videoBytes = new Map();
      const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, "0")).join("");
      this.audioSession = `${id.slice(0, 8)}-${id.slice(8, 12)}-${id.slice(12, 16)}-${id.slice(16, 20)}-${id.slice(20)}`;
      this.audioGeneration = 0;
      this.sources = [];
      this.bufferQueue = Promise.resolve();
      this.status = document.getElementById("playback-status");
      this.controls = document.getElementById("precise-controls");
      this.installControls();
      this.ready = this.initialize();
      this.ready.catch(error => this.fail(error));
      this.timer = setInterval(() => this.maintain(), 150);
      video.addEventListener("ended", () => {
        if (this.manifest.audio && this.manifest.duration > this.manifest.videoDuration) {
          this.holdingVideo = true;
        } else this.pause();
      });
      video.addEventListener("pause", () => {
        if (this.wanted && !this.starting && !this.seekPending && !video.ended && video.paused) this.pause();
      });
      video.addEventListener("play", () => {
        // Some media backends finish a pending play after pause() returned.
        // The latest user intent wins, including immediately after a rate change.
        if (!this.wanted) { this.video.pause(); this.stopAudio(); }
      });
      video.addEventListener("waiting", () => {
        if (this.wanted && !this.starting && !this.seekPending && !this.scrubbing) this.recover();
      });
      document.addEventListener("visibilitychange", () => { if (document.hidden) this.pause(); });
      window.addEventListener("pagehide", () => {
        this.pause(); clearInterval(this.timer); this.context?.close().catch(() => {}); this.audio.clear();
        this.audioAbort?.abort();
        this.cancelVideoRequests(-1);
        this.lifetime.abort();
        fetch(`${this.base}/audio-session/${this.audioSession}?generation=${this.audioGeneration}`, { method: "DELETE", keepalive: true,
          headers: { "X-Mogao-Request": "1" } }).catch(() => {});
      });
      window.addEventListener("pageshow", e => { if (e.persisted) {
        if (!this.initialized) { location.reload(); return; }
        this.lifetime = new AbortController();
        if (this.manifest) this.resetAudio(this.currentTime);
        this.timer = setInterval(() => this.maintain(), 150);
      } });
      video.addEventListener("error", () => this.fail(new Error("The browser could not decode this video.")));
    }

    async fetch(path, binary = true, options = {}) {
      const { maxBytes = 8 * 1024 * 1024, signal = AbortSignal.timeout(120000), ...requestOptions } = options;
      const response = await fetch(path, { ...requestOptions,
        signal: AbortSignal.any([signal, this.lifetime.signal]) });
      if (!response.ok) {
        let detail;
        try { detail = (await response.json()).detail; } catch { /* Use the generic error below. */ }
        const error = new Error(typeof detail === "string" ? detail : detail?.message || "Could not load playback data. Please retry.");
        error.code = detail?.code;
        throw error;
      }
      if (!binary) return response.json();
      const reader = response.body.getReader(), parts = [];
      let size = 0;
      try {
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          size += value.byteLength;
          if (size > maxBytes) throw new Error("A media fragment exceeds the buffer budget.");
          parts.push(value);
        }
      } finally { await reader.cancel(); reader.releaseLock(); }
      const bytes = new Uint8Array(size);
      let offset = 0;
      for (const part of parts) { bytes.set(part, offset); offset += part.byteLength; }
      return bytes.buffer;
    }

    fetchVideo(path, options) {
      const request = (this.videoFetchQueue || Promise.resolve()).catch(() => {}).then(() => {
        options.signal.throwIfAborted();
        return this.fetch(path, true, { ...options, maxBytes: VIDEO_BYTES });
      });
      this.videoFetchQueue = request.then(() => {}, () => {});
      return request;
    }

    async initialize() {
      this.setStatus("Loading video…");
      this.manifest = await this.fetch(`${this.base}/manifest`, false, { signal: AbortSignal.timeout(480000) });
      this.assetBase = `${this.base}/${this.manifest.version}`;
      const Source = window.ManagedMediaSource || window.MediaSource;
      if (!Source?.isTypeSupported(this.manifest.mime)) throw new Error("This browser cannot decode the video's format.");
      const Offline = window.OfflineAudioContext || window.webkitOfflineAudioContext;
      if (this.manifest.audio && !Offline) throw new Error("This browser does not support audio playback.");
      if (this.manifest.audio) this.decoder = new Offline(AUDIO_CHANNELS, 1, this.manifest.sampleRate);
      this.mediaSource = new Source();
      this.url = URL.createObjectURL(this.mediaSource);
      await event(this.mediaSource, "sourceopen", () => { this.video.src = this.url; });
      this.buffer = this.mediaSource.addSourceBuffer(this.manifest.mime);
      const initial = await this.fetch(`${this.assetBase}/video/0`);
      await this.append(initial);
      this.mediaSource.duration = this.manifest.duration;
      const initialPosition = this.pendingTime;
      const position = this.clampPosition(this.pendingTime);
      this.resetAudio(position);
      await Promise.all([this.ensureVideo(position), this.ensureAudio(position, this.rate)]);
      await this.moveVideo(position);
      if (this.pendingTime === initialPosition) {
        this.pendingTime = position;
        this.holdingVideo = position >= this.manifest.videoDuration;
        this.seekPending = false;
      }
      document.getElementById("precise-rate").replaceChildren(
        ...this.manifest.rates.map(rate => new Option(`${rate}×`, String(rate), rate === 1, rate === this.rate)));
      this.setStatus(""); this.controls.querySelectorAll("input,button,select").forEach(control => { control.disabled = false; });
      document.getElementById("precise-seek").max = String(this.manifest.duration);
      document.getElementById("precise-volume").disabled = !this.manifest.audio;
      document.getElementById("precise-mute").disabled = !this.manifest.audio;
      document.getElementById("precise-fullscreen").disabled = !document.body.requestFullscreen;
      this.initialized = true;
      this.updateControls();
      this.prefetch();
    }

    append(bytes) {
      const operation = this.bufferQueue.catch(() => {}).then(() =>
        event(this.buffer, "updateend", () => this.buffer.appendBuffer(bytes)));
      this.bufferQueue = operation;
      return operation;
    }

    clampPosition(position) {
      return Math.max(0, Math.min(position, this.manifest.duration - 0.05));
    }

    segmentIndex(position) {
      // Treat positions within 1 ms of the boundary as belonging to the next GOP.
      return this.manifest.segments.findIndex(segment => segment.start + segment.duration > position + 0.001);
    }

    cancelVideoRequests(position) {
      for (const request of this.segmentRequests.values()) {
        if (position < request.segment.start || position >= request.segment.start + request.segment.duration) {
          request.controller.abort();
        }
      }
    }

    async ensureVideo(position, ahead = 0, speculative = false) {
      const revision = this.revision, segments = this.manifest.segments;
      let index = this.segmentIndex(position);
      if (index < 0) index = segments.length - 1;
      for (let i = index; i <= Math.min(index + ahead, segments.length - 1); i++) {
        if (revision !== this.revision) return;
        const segment = segments[i];
        if (this.videoBytes.has(segment.number)) {
          // ManagedMediaSource may evict data without an explicit remove().
          const ranges = this.buffer.buffered;
          if (Array.from({ length: ranges.length }, (_, n) => n).some(n =>
            ranges.start(n) <= segment.start + 0.05 && ranges.end(n) >= segment.start + segment.duration - 0.05)) continue;
          this.videoBytes.delete(segment.number);
        }
        const used = [...this.videoBytes.values()].reduce((sum, size) => sum + size, 0);
        if (speculative && segment.bytes != null && used + segment.bytes > VIDEO_BYTES) continue;
        let request = this.segmentRequests.get(segment.number);
        if (request?.controller.signal.aborted) request = null;
        if (!request) {
          const controller = new AbortController();
          request = { controller, segment, required: !speculative };
          request.promise = this.fetchVideo(`${this.assetBase}/video/${segment.number}`,
            { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(120000)]), priority: ahead ? "low" : "high" })
            .then(async bytes => {
              if (controller.signal.aborted) return;
              segment.bytes = bytes.byteLength;
              const append = this.bufferQueue.catch(() => {}).then(async () => {
                if (controller.signal.aborted) return;
                const used = [...this.videoBytes.values()].reduce((sum, size) => sum + size, 0);
                // Speculative loads must not evict and refetch each other.
                if (!request.required && used + bytes.byteLength > VIDEO_BYTES) return;
                if (request.required) await this.makeVideoRoom(bytes.byteLength, this.currentTime, segment.number);
                if (controller.signal.aborted) return;
                await event(this.buffer, "updateend", () => this.buffer.appendBuffer(bytes));
                this.videoBytes.set(segment.number, bytes.byteLength);
              });
              this.bufferQueue = append; await append;
            }).finally(() => {
              if (this.segmentRequests.get(segment.number) === request) this.segmentRequests.delete(segment.number);
            });
          this.segmentRequests.set(segment.number, request);
        }
        if (!speculative) request.required = true;
        try { await request.promise; }
        catch (error) { if (revision !== this.revision || error.name === "AbortError") return; throw error; }
      }
      if (revision === this.revision && this.videoBytes.has(segments[segments.length - 1].number)) {
        const operation = this.bufferQueue.catch(() => {}).then(() => {
          if (this.mediaSource.readyState === "open" && revision === this.revision) this.mediaSource.endOfStream();
        });
        this.bufferQueue = operation; await operation;
      }
    }

    chunkAt(position) { return Math.floor(position / this.manifest.chunkSeconds); }
    audioWindow(position) {
      const first = this.chunkAt(Math.max(0, position - BUFFER_BEHIND));
      const bytesPerChunk = this.manifest.chunkSeconds * this.manifest.sampleRate * AUDIO_CHANNELS * FLOAT_BYTES * (1 / this.normalRate + 1 / 2);
      const count = Math.floor(AUDIO_BYTES / bytesPerChunk);
      return {
        first,
        last: Math.min(first + count - 1, this.chunkAt(Math.min(this.manifest.duration - 0.001, position + BUFFER_AHEAD))),
      };
    }
    resetAudio(position) {
      this.audioAbort?.abort();
      this.audioAbort = new AbortController();
      this.audio.clear();
      this.audioGeneration++;
      this.audioMisses = new Map();
      // Warm the Back button even after starting at an unbuffered position.
      this.audioOrigin = this.audioWindow(position).first;
      this.scheduling = null;
    }
    audioChunk(chunk, rate) {
      const key = `${rate}:${chunk}`;
      if (this.audioMisses.has(key)) return Promise.reject(this.audioMisses.get(key));
      if (!this.audio.has(key)) {
        const signal = this.audioAbort.signal;
        const path = `${this.assetBase}/audio/${chunk}?rate=${rate}&session=${this.audioSession}&generation=${this.audioGeneration}&origin=${this.audioOrigin}`;
        const request = (this.audioQueue || Promise.resolve()).catch(() => {}).then(async () => {
            signal.throwIfAborted();
            const bytes = await this.fetch(path, true,
              { signal: AbortSignal.any([signal, AbortSignal.timeout(45000)]) });
            signal.throwIfAborted();
            return this.decoder.decodeAudioData(bytes);
          }).then(buffer => { signal.throwIfAborted(); return buffer; });
        this.audioQueue = request.then(() => {}, () => {});
        this.audio.set(key, request);
        request.catch(error => {
          if (this.audio.get(key) === request) this.audio.delete(key);
          if (!signal.aborted && this.audioAbort.signal === signal &&
              ["AUDIO_WINDOW_EXPIRED", "AUDIO_SUPERSEDED"].includes(error.code)) {
            this.audioMisses.set(key, error);
          }
        });
      }
      return this.audio.get(key);
    }
    async ensureAudio(position, rate, ahead = 0) {
      if (!this.manifest.audio) return;
      const index = this.chunkAt(position);
      if (((index + 1) * this.manifest.chunkSeconds - position) / rate < 1) ahead = Math.max(ahead, 1);
      await Promise.all(Array.from({ length: ahead + 1 }, (_, offset) => index + offset)
        .filter(chunk => chunk * this.manifest.chunkSeconds < this.manifest.duration)
        .map(chunk => this.audioChunk(chunk, rate)));
    }

    async prepareAudio(position, rate, revision) {
      // Seek/resume call this after ready: an early transcript jump must not
      // abort the audio initialization is still waiting for.
      if (this.chunkAt(position) < this.audioOrigin) this.resetAudio(position);
      try { await this.ensureAudio(position, rate); }
      catch (error) {
        if (error.code !== "AUDIO_WINDOW_EXPIRED" || revision !== this.revision) throw error;
        // An expired forward-only decoder needs a new generation.
        this.resetAudio(position);
        await this.ensureAudio(position, rate);
      }
    }

    ensureContext() {
      if (!this.context || this.context.state === "closed") {
        const Context = window.AudioContext || window.webkitAudioContext;
        this.context = new Context({ sampleRate: this.manifest?.sampleRate, latencyHint: "interactive" });
        this.context.addEventListener("statechange", () => {
          if (this.wanted && this.context.state !== "running") this.pause();
        });
        this.gain = this.context.createGain();
        this.gain.gain.value = this.muted ? 0 : this.volume;
        this.gain.connect(this.context.destination);
      }
      return this.context;
    }

    stopAudio() {
      for (const source of this.sources) { source.stop(); source.disconnect(); }
      this.sources = [];
      this.audioEpoch = null;
      this.scheduling = null;
    }

    outputTime() {
      const stamp = this.context.getOutputTimestamp?.();
      if (stamp?.contextTime > 0 && stamp.performanceTime > 0) {
        return Math.min(this.context.currentTime,
          stamp.contextTime + Math.max(0, performance.now() - stamp.performanceTime) / 1000);
      }
      return this.context.currentTime - (this.context.outputLatency || this.context.baseLatency || 0.025);
    }

    async startAudio(position, revision) {
      if (!this.manifest.audio) return;
      this.stopAudio();
      this.audioEpoch = { position, time: this.context.currentTime, rate: this.rate };
      this.nextChunk = this.chunkAt(position);
      await this.scheduleAudio(revision, 1);
    }

    async scheduleAudio(revision, secondsAhead = 3) {
      const epoch = this.audioEpoch;
      if (!epoch) return;
      const horizon = this.context.currentTime + secondsAhead;
      while (this.nextChunk * this.manifest.chunkSeconds < this.manifest.duration) {
        const chunk = this.nextChunk;
        const mediaStart = Math.max(epoch.position, chunk * this.manifest.chunkSeconds);
        const when = epoch.time + (mediaStart - epoch.position) / epoch.rate;
        if (when > horizon) break;
        const buffer = await this.audioChunk(chunk, epoch.rate);
        if (revision !== this.revision || this.audioEpoch !== epoch) return;
        if (when < this.context.currentTime - 0.04) throw new Error("Audio buffering fell behind. Press Play to resume.");
        const source = this.context.createBufferSource();
        source.buffer = buffer;
        // Tempo was changed continuously on the server. Playback rate stays 1
        // here, preserving pitch and sample continuity across chunk boundaries.
        source.connect(this.gain);
        source.start(when, (mediaStart - chunk * this.manifest.chunkSeconds) / epoch.rate);
        this.sources.push(source);
        source.onended = () => { source.disconnect(); this.sources = this.sources.filter(item => item !== source); };
        this.nextChunk++;
      }
    }

    async moveVideo(position) {
      // Keep the immediate successor loading, but defer the wider window until
      // the seek completes. In particular, don't prepend older fragments while
      // the decoder is seeking inside an already buffered range.
      position = Math.min(position, Math.max(0, this.manifest.videoDuration - 0.001));
      if (this.initialized) this.ensureVideo(position, 1).catch(() => {});
      if (Math.abs(this.video.currentTime - position) > 0.005 || this.video.seeking) {
        await event(this.video, "seeked", () => { this.video.currentTime = position; });
      }
    }

    play() {
      document.getElementById("precise-retry").hidden = true;
      this.wanted = true;
      if (this.manifest && (this.currentTime >= this.manifest.duration || (this.pausedAt != null && performance.now() - this.pausedAt > 30000))) {
        this.resetAudio(this.currentTime >= this.manifest.duration ? 0 : this.currentTime);
      }
      this.pausedAt = null;
      // Audio authorization must happen in the actual user gesture, before awaits.
      let authorization;
      try { authorization = this.ensureContext().resume(); }
      catch (error) { this.fail(error); return Promise.resolve(); }
      const revision = ++this.revision;
      this.updateControls();
      return this.resume(revision, authorization).catch(error => {
        if (revision === this.revision) this.fail(error);
      });
    }

    async resume(revision, authorization) {
      this.starting = true;
      const loading = setTimeout(() => {
        if (revision === this.revision && this.wanted) this.setStatus("Buffering…");
      }, 250);
      try {
        await this.ready;
        await timeout(authorization || this.context.resume(), "Could not start audio.");
        if (!this.wanted || revision !== this.revision) return;
        const position = this.seekPending ? this.clampPosition(this.pendingTime) :
          this.currentTime >= this.manifest.duration ? 0 : this.currentTime;
        this.video.pause();
        this.stopAudio();
        const segment = this.manifest.segments[this.segmentIndex(position)];
        // Seeking already ran alongside lookahead. If the target has less than
        // a second left, finish loading its successor before starting playback
        // so the jump does not immediately turn into another stall.
        const ahead = segment && segment.start + segment.duration - position < this.rate ? 1 : 0;
        await Promise.all([
          this.ensureVideo(position, ahead),
          this.prepareAudio(position, this.rate, revision),
        ]);
        if (!this.wanted || revision !== this.revision) return;
        await this.moveVideo(position);
        if (!this.wanted || revision !== this.revision) return;
        this.seekPending = false;
        this.video.playbackRate = this.rate;
        this.holdingVideo = position >= this.manifest.videoDuration;
        if (!this.holdingVideo) await timeout(this.video.play(), "Could not start video.");
        if (!this.wanted || revision !== this.revision) {
          if (!this.wanted) this.video.pause();
          return;
        }
        await this.startAudio(position, revision);
        if (!this.wanted || revision !== this.revision) return;
        this.setStatus("");
        this.prefetch();
      } finally { clearTimeout(loading); if (revision === this.revision) this.starting = false; this.updateControls(); }
    }

    pause() {
      this.pendingTime = this.currentTime;
      this.pausedAt ??= performance.now();
      this.wanted = false;
      this.revision++;
      this.starting = false;
      this.video.pause();
      this.stopAudio();
      this.setStatus("");
      this.updateControls();
    }

    get currentTime() {
      if (this.holdingVideo && !this.seekPending) {
        return this.audioEpoch ? Math.min(this.manifest.duration,
          this.audioEpoch.position + Math.max(0, this.outputTime() - this.audioEpoch.time) * this.audioEpoch.rate) : this.pendingTime;
      }
      return this.seekPending ? Math.min(this.pendingTime, this.manifest?.duration ?? Infinity) : this.video.currentTime;
    }
    set currentTime(value) {
      this.pendingTime = Math.max(0, Number(value) || 0);
      if (this.manifest) this.pendingTime = this.clampPosition(this.pendingTime);
      this.seekPending = true;
      const revision = ++this.revision;
      this.starting = false;
      // Initialization must finish loading its target before queued seeks can
      // run. Cancelling that fragment would leave ready waiting on an empty seek.
      if (this.initialized) this.cancelVideoRequests(this.pendingTime);
      this.video.pause();
      this.stopAudio();
      this.updateControls();
      this.seek(this.pendingTime, revision).catch(error => { if (revision === this.revision) this.fail(error); });
    }
    async seek(position, revision) {
      const loading = setTimeout(() => { if (revision === this.revision) this.setStatus("Buffering…"); }, 250);
      try {
        await this.ready;
        position = this.clampPosition(position);
        if (revision !== this.revision) return;
        await Promise.all([
          (async () => {
            await this.ensureVideo(position);
            if (revision === this.revision) await this.moveVideo(position);
          })(),
          this.prepareAudio(position, this.rate, revision),
        ]);
        if (revision !== this.revision) return;
        this.holdingVideo = position >= this.manifest.videoDuration;
        this.seekPending = false;
        this.setStatus("");
        if (this.wanted) await this.resume(revision);
        this.updateControls();
        this.prefetch();
      } finally { clearTimeout(loading); }
    }

    get playbackRate() { return this.rate; }
    set playbackRate(value) {
      if (!this.manifest?.rates.includes(Number(value)) || Number(value) === this.rate) return;
      const position = this.currentTime;
      this.pendingTime = position;
      const revision = ++this.revision;
      this.rate = Number(value);
      if (this.rate !== 2) this.normalRate = this.rate;
      if (!this.audio.has(`${this.rate}:${this.chunkAt(this.currentTime)}`)) this.resetAudio(this.currentTime);
      this.video.playbackRate = this.rate;
      if (this.wanted) {
        this.video.pause();
        this.stopAudio();
        this.resume(revision).catch(error => { if (revision === this.revision) this.fail(error); });
      } else if (this.seekPending) {
        this.seek(position, revision).catch(error => { if (revision === this.revision) this.fail(error); });
      }
      this.updateControls();
      this.prefetch();
    }

    recover() {
      this.pendingTime = this.currentTime;
      this.video.pause();
      this.stopAudio();
      const revision = ++this.revision;
      this.resume(revision).catch(error => { if (revision === this.revision) this.fail(error); });
    }

    playbackError(error, revision) {
      if (revision !== this.revision) return;
      if (error.code === "AUDIO_WINDOW_EXPIRED") {
        this.resetAudio(this.currentTime);
        this.recover();
      } else this.fail(error);
    }

    prefetch() {
      if (!this.initialized || this.seekPending || this.scrubbing) return;
      const position = this.currentTime, { first, last } = this.audioWindow(position);
      const rates = new Set([this.normalRate, 2]);
      for (const key of this.audio.keys()) {
        const chunk = Number(key.split(":")[1]);
        const rate = Number(key.split(":")[0]);
        if (chunk < first || chunk > last || !rates.has(rate)) this.audio.delete(key);
      }
      for (const key of this.audioMisses.keys()) {
        const [rate, chunk] = key.split(":").map(Number);
        if (chunk < first || chunk > last || !rates.has(rate)) this.audioMisses.delete(key);
      }
      if (this.manifest.audio) {
        this.ensureAudio(position, this.rate, 1).catch(() => {});
        this.prefetchAudio(position).catch(() => {});
      }
      const revision = this.revision;
      this.prefetchVideo(position).catch(error => { if (this.wanted && revision === this.revision) this.fail(error); });
    }

    prefetchAudio(position) {
      const revision = this.revision, generation = this.audioGeneration;
      if (this.audioWarming?.revision === revision && this.audioWarming.generation === generation) return this.audioWarming.promise;
      const warming = { revision, generation };
      warming.promise = (async () => {
        const { first, last } = this.audioWindow(position), current = this.chunkAt(position);
        const forward = this.chunkAt(position + JUMP_SECONDS), back = this.chunkAt(Math.max(0, position - JUMP_SECONDS));
        const chunks = new Set([current, current + 1, forward, forward + 1, back,
          ...Array.from({ length: last - first + 1 }, (_, i) => first + i)]);
        for (const chunk of chunks) {
          if (chunk < Math.max(first, this.audioOrigin) || chunk > last) continue;
          for (const rate of new Set([this.rate, this.normalRate, 2])) {
            if (revision !== this.revision || generation !== this.audioGeneration) return;
            // Queue one background request at a time so a user action can run
            // after the in-flight transfer, not behind a minute of prefetch.
            await this.audioChunk(chunk, rate).catch(() => {});
          }
        }
      })().finally(() => { if (this.audioWarming === warming) this.audioWarming = null; });
      this.audioWarming = warming;
      return warming.promise;
    }

    prefetchVideo(position) {
      const revision = this.revision;
      if (this.videoWarming?.revision === revision) return this.videoWarming.promise;
      const warming = { revision };
      warming.promise = (async () => {
        // The immediate successor is required for uninterrupted playback even
        // at the byte limit. Everything farther away remains speculative.
        await this.ensureVideo(position, 1);
        const segments = this.manifest.segments;
        const forward = segments.filter(s => s.start > position && s.start < position + BUFFER_AHEAD);
        const backward = segments.filter(s => s.start + s.duration <= position &&
          s.start + s.duration > position - BUFFER_BEHIND).reverse();
        const nearEnd = position + JUMP_SECONDS + 2 * this.rate;
        const groups = [
          { direction: "forward", segments: forward.filter(s => s.start < nearEnd) },
          { direction: "backward", segments: backward.filter(s => s.start + s.duration > position - JUMP_SECONDS) },
          { direction: "forward", segments: forward.filter(s => s.start >= nearEnd) },
          { direction: "backward", segments: backward.filter(s => s.start + s.duration <= position - JUMP_SECONDS) },
        ];
        const blocked = new Set();
        for (const { direction, segments } of groups) {
          if (blocked.has(direction)) continue;
          for (const segment of segments) {
            if (revision !== this.revision) return;
            await this.ensureVideo(segment.start, 0, true);
            // Do not fill distant islands beyond a gap that cannot fit in RAM.
            if (!this.videoBytes.has(segment.number)) { blocked.add(direction); break; }
          }
        }
      })().finally(() => { if (this.videoWarming === warming) this.videoWarming = null; });
      this.videoWarming = warming;
      return warming.promise;
    }

    async trimVideo() {
      if (this.trimming || !this.buffer || this.buffer.updating) return;
      const position = this.video.currentTime;
      const segments = this.manifest.segments;
      const before = Math.max(0, ...segments.filter(s => s.start + s.duration < position - BUFFER_BEHIND).map(s => s.start + s.duration));
      const after = segments.find(s => s.start > position + BUFFER_AHEAD)?.start ?? this.manifest.duration;
      const ranges = [];
      if (before > 0 && this.buffer.buffered.length && this.buffer.buffered.start(0) < before - 8) ranges.push([0, before]);
      const end = this.buffer.buffered.length ? this.buffer.buffered.end(this.buffer.buffered.length - 1) : 0;
      if (end > after + 8) ranges.push([after, end]);
      if (!ranges.length) return;
      this.trimming = true;
      const revision = this.revision;
      try {
        const operation = this.bufferQueue.catch(() => {}).then(async () => {
          for (const [start, end] of ranges) {
            if (revision !== this.revision) return;
            await event(this.buffer, "updateend", () => this.buffer.remove(start, end));
            for (const segment of this.manifest.segments) {
              if (segment.start >= start && segment.start + segment.duration <= end) {
                this.videoBytes.delete(segment.number);
              }
            }
          }
        });
        this.bufferQueue = operation; await operation;
      } finally { this.trimming = false; }
    }

    async makeVideoRoom(bytes, position, number) {
      if (bytes > VIDEO_BYTES) throw new Error("A video keyframe interval exceeds the buffer budget.");
      let total = [...this.videoBytes.values()].reduce((sum, size) => sum + size, 0);
      const segments = this.manifest.segments.filter(s => this.videoBytes.has(s.number) && s.number !== number &&
        !(position >= s.start && position < s.start + s.duration))
        .sort((a, b) => Math.abs(b.start - position) - Math.abs(a.start - position));
      for (const segment of segments) {
        if (total + bytes <= VIDEO_BYTES) break;
        await event(this.buffer, "updateend", () => this.buffer.remove(segment.start, segment.start + segment.duration));
        total -= this.videoBytes.get(segment.number);
        this.videoBytes.delete(segment.number);
      }
      if (total + bytes > VIDEO_BYTES) {
        const current = this.manifest.segments.findIndex(s => position >= s.start && position < s.start + s.duration);
        // Removing part of a GOP can also remove its dependent frames through
        // the next keyframe. Keep the current GOP and its required successor
        // together instead. All other fragments have been evicted above, so
        // this working set is bounded by two per-fragment limits (128 MiB).
        if (current >= 0 && this.manifest.segments[current + 1]?.number === number) return;
        throw new Error("Video buffer is full. Please retry.");
      }
    }

    maintain() {
      this.updateControls();
      if (!this.initialized || this.seekPending || this.scrubbing) return;
      if (this.wanted && !this.starting && this.context?.state !== "running") {
        this.pause();
        return;
      }
      if (this.wanted && this.holdingVideo && this.currentTime >= this.manifest.duration) {
        this.pause();
        return;
      }
      if (this.holdingVideo) this.video.dispatchEvent(new Event("timeupdate"));
      if (this.wanted && !this.starting && (!this.video.paused || this.holdingVideo)) {
        if (this.audioEpoch) {
          const audible = this.audioEpoch.position + Math.max(0, this.outputTime() - this.audioEpoch.time) * this.rate;
          const error = audible - this.video.currentTime;
          // Correct clock drift through gentle video pacing, without seeking
          // either stream forward and losing speech at resume.
          const correction = Math.abs(error) < 0.015 ? 0 : Math.max(-0.03, Math.min(0.03, error * 0.2));
          if (!this.holdingVideo) this.video.playbackRate = this.rate * (1 + correction);
          if (!this.scheduling) {
            const revision = this.revision;
            const task = this.scheduleAudio(revision)
              .catch(error => this.playbackError(error, revision))
              .finally(() => { if (this.scheduling === task) this.scheduling = null; });
            this.scheduling = task;
          }
        }
        if (!this.prefetching) {
          this.prefetching = true;
          const revision = this.revision;
          Promise.all([this.prefetchVideo(this.currentTime), this.ensureAudio(this.currentTime, this.rate, 1)])
            .catch(error => this.playbackError(error, revision)).finally(() => { this.prefetching = false; });
        }
      }
      if (!this.lastPrune || performance.now() - this.lastPrune > 2000) {
        this.lastPrune = performance.now(); this.prefetch(); this.trimVideo().catch(error => this.fail(error));
      }
    }

    setStatus(text, error = false) {
      this.status.querySelector(".playback-message").textContent = text;
      this.status.classList.toggle("playback-error", error);
      this.status.hidden = !text;
      this.video.setAttribute("aria-busy", String(Boolean(text) && !error));
    }
    fail(error) {
      this.pause();
      if (this.manifest) this.resetAudio(this.currentTime);
      this.setStatus(error.message || "Playback failed. Please retry.", true);
      document.getElementById("precise-retry").hidden = false;
    }
    formatTime(time) {
      const seconds = Math.max(0, Math.floor(time || 0));
      const minutes = Math.floor(seconds / 60);
      return minutes >= 60 ? `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}` :
        `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
    }
    paintRange(input) {
      const fraction = (Number(input.value) - Number(input.min)) / (Number(input.max) - Number(input.min) || 1);
      input.style.setProperty("--range-progress", `${fraction * 100}%`);
    }
    updateControls() {
      const play = document.getElementById("precise-play");
      play.dataset.playing = String(this.wanted);
      play.setAttribute("aria-label", this.wanted ? "Pause" : "Play");
      play.title = this.wanted ? "Pause" : "Play";
      const seek = document.getElementById("precise-seek");
      if (!this.scrubbing) seek.value = String(this.currentTime);
      const position = this.scrubbing ? Number(seek.value) : this.currentTime;
      this.paintRange(seek);
      seek.setAttribute("aria-valuetext", `${this.formatTime(position)} of ${this.formatTime(this.manifest?.duration)}`);
      document.getElementById("precise-time").textContent = `${this.formatTime(position)} / ${this.formatTime(this.manifest?.duration)}`;
      document.getElementById("precise-rate").value = String(this.rate);
    }
    updateVolume() {
      const input = document.getElementById("precise-volume");
      input.value = String(this.muted ? 0 : this.volume);
      input.setAttribute("aria-valuetext", `${Math.round(Number(input.value) * 100)}%`);
      this.paintRange(input);
      const mute = document.getElementById("precise-mute");
      mute.dataset.muted = String(this.muted || this.volume === 0);
      mute.setAttribute("aria-label", this.muted || this.volume === 0 ? "Unmute" : "Mute");
      mute.title = mute.getAttribute("aria-label");
      if (this.gain) this.gain.gain.setTargetAtTime(this.muted ? 0 : this.volume, this.context.currentTime, 0.015);
    }
    bindRange(input, { start = () => {}, input: change, end = () => {} }) {
      let pointer = null;
      const move = event => {
        const rect = input.getBoundingClientRect(), inset = 8;
        const fraction = Math.max(0, Math.min(1, (event.clientX - rect.left - inset) / (rect.width - inset * 2)));
        const value = Number(input.min) + fraction * (Number(input.max) - Number(input.min));
        input.value = String(Math.round(value / Number(input.step)) * Number(input.step));
        change(); this.paintRange(input);
      };
      input.addEventListener("pointerdown", event => {
        if (input.disabled || !event.isPrimary || event.button !== 0) return;
        event.preventDefault(); input.focus({ preventScroll: true });
        pointer = event.pointerId; input.setPointerCapture(pointer); start(); move(event);
      });
      input.addEventListener("pointermove", event => { if (event.pointerId === pointer) move(event); });
      const finish = event => {
        if (event.pointerId !== pointer) return;
        const id = pointer; pointer = null;
        if (input.hasPointerCapture(id)) input.releasePointerCapture(id);
        end();
      };
      for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) input.addEventListener(name, finish);
      // Retain native keyboard and assistive-technology range interaction.
      input.addEventListener("input", () => { if (pointer === null) { start(); change(); this.paintRange(input); } });
      input.addEventListener("change", () => { if (pointer === null) end(); });
    }
    installControls() {
      this.volume = 1;
      this.muted = false;
      document.getElementById("precise-play").addEventListener("click", () => { this.wanted ? this.pause() : this.play(); });
      document.getElementById("precise-retry").addEventListener("click", () => location.reload());
      this.video.addEventListener("click", () => { this.wanted ? this.pause() : this.play(); });
      const seek = document.getElementById("precise-seek");
      this.bindRange(seek, {
        start: () => {
          if (this.scrubbing) return;
          this.scrubWasPlaying = this.wanted;
          this.scrubbing = true;
          this.pause();
          this.cancelVideoRequests(-1);
        },
        input: () => this.updateControls(),
        end: () => {
          if (!this.scrubbing) return;
          this.scrubbing = false;
          this.currentTime = seek.value;
          if (this.scrubWasPlaying) this.play();
        },
      });
      for (const [id, offset] of [["precise-backward", -JUMP_SECONDS], ["precise-forward", JUMP_SECONDS]]) {
        document.getElementById(id).addEventListener("click", () => {
          this.currentTime = this.clampPosition(this.currentTime + offset);
        });
      }
      document.getElementById("precise-rate").addEventListener("change", e => { this.playbackRate = e.target.value; });
      this.bindRange(document.getElementById("precise-volume"), {
        input: () => {
          this.volume = Number(document.getElementById("precise-volume").value);
          this.muted = false;
          this.updateVolume();
        },
      });
      document.getElementById("precise-mute").addEventListener("click", () => {
        if (this.volume === 0) { this.volume = 1; this.muted = false; }
        else this.muted = !this.muted;
        this.updateVolume();
      });
      this.updateVolume();
      document.getElementById("precise-fullscreen").addEventListener("click", () => {
        const container = document.body;
        if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
        else container.requestFullscreen?.().catch(() => {});
      });
    }
  }

  window.createVideoPlayback = (video, config) => new PrecisePlayback(video, config);
})();
