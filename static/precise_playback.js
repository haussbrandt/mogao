/* Video-only Media Source playback with bounded, separately decoded audio. */
(() => {
  "use strict";
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
      this.rate = 1; this.wanted = false; this.revision = 0;
      this.audio = new Map(); this.videoSegments = new Set(); this.segmentRequests = new Map();
      this.sources = []; this.bufferQueue = Promise.resolve(); this.closed = false;
      this.status = document.getElementById("playback-status");
      this.controls = document.getElementById("precise-controls");
      this.controls.hidden = false;
      document.getElementById("player-container").classList.add("precise-playback");
      this.video.controls = false; this.video.muted = true; this.video.disableRemotePlayback = true;
      this.video.removeAttribute("src"); this.video.load();
      this.installControls();
      this.ready = this.initialize();
      this.ready.catch(error => this.fail(error));
      this.timer = setInterval(() => this.maintain(), 150);
      video.addEventListener("ended", () => { this.wanted = false; this.stopAudio(); this.updateControls(); });
      video.addEventListener("play", () => {
        // Some media backends finish a pending play after pause() returned.
        // The latest user intent wins, including immediately after a rate change.
        if (!this.wanted) { this.video.pause(); this.stopAudio(); }
      });
      video.addEventListener("waiting", () => {
        if (this.wanted && !this.starting && !this.seekPending && !this.scrubbing) this.recover();
      });
      document.addEventListener("visibilitychange", () => { if (document.hidden) this.pause(); });
      this.onPageHide = () => { this.pause(); clearInterval(this.timer); this.context?.close().catch(() => {}); this.audio.clear(); };
      window.addEventListener("pagehide", this.onPageHide);
      window.addEventListener("pageshow", e => { if (e.persisted) this.timer = setInterval(() => this.maintain(), 150); });
      video.addEventListener("error", () => this.fail(new Error("The browser could not decode this video.")));
    }

    async fetch(path, binary = true, options = {}) {
      const response = await fetch(path, { signal: AbortSignal.timeout(120000), ...options });
      if (!response.ok) {
        let detail;
        try { detail = (await response.json()).detail; } catch { /* Use the generic error below. */ }
        throw new Error(detail || "Could not load playback data. Please retry.");
      }
      return binary ? response.arrayBuffer() : response.json();
    }

    async initialize() {
      this.setStatus("Loading video…");
      this.manifest = await this.fetch(`${this.base}/manifest`, false);
      this.assetBase = `${this.base}/${this.manifest.version}`;
      const Source = window.ManagedMediaSource || window.MediaSource;
      if (!Source?.isTypeSupported(this.manifest.mime)) throw new Error("This browser cannot decode the video's format.");
      const Offline = window.OfflineAudioContext || window.webkitOfflineAudioContext;
      if (this.manifest.audio && !Offline) throw new Error("This browser does not support audio playback.");
      if (this.manifest.audio) this.decoder = new Offline(2, 1, this.manifest.sampleRate);
      this.mediaSource = new Source();
      this.url = URL.createObjectURL(this.mediaSource);
      await event(this.mediaSource, "sourceopen", () => { this.video.src = this.url; });
      this.buffer = this.mediaSource.addSourceBuffer(this.manifest.mime);
      const initial = await this.fetch(`${this.assetBase}/video/0`);
      await this.append(initial);
      this.mediaSource.duration = this.manifest.duration;
      const initialPosition = this.pendingTime;
      const position = Math.min(this.pendingTime, Math.max(0, this.manifest.duration - 0.05));
      await Promise.all([this.ensureVideo(position), this.ensureAudio(position, this.rate)]);
      await this.moveVideo(position);
      if (this.pendingTime === initialPosition) this.seekPending = false;
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

    cancelVideoRequests(position) {
      for (const request of this.segmentRequests.values()) {
        if (position < request.segment.start || position >= request.segment.start + request.segment.duration) {
          request.controller.abort();
        }
      }
    }

    async ensureVideo(position, ahead = 0) {
      const revision = this.revision, segments = this.manifest.segments;
      let index = segments.findIndex(segment => segment.start + segment.duration > position + 0.001);
      if (index < 0) index = segments.length - 1;
      if (segments[index].start + segments[index].duration - position < this.rate) ahead = Math.max(ahead, 1);
      // DASH fragments start at keyframes. Seeking needs only the target
      // fragment; loading the preceding fragment doubled large-video waits.
      for (let i = index; i <= Math.min(index + ahead, segments.length - 1); i++) {
        if (revision !== this.revision) return;
        const segment = segments[i];
        if (this.videoSegments.has(segment.number)) {
          // ManagedMediaSource may evict data without an explicit remove().
          const ranges = this.buffer.buffered;
          if (Array.from({ length: ranges.length }, (_, n) => n).some(n =>
            ranges.start(n) <= segment.start + 0.05 && ranges.end(n) >= segment.start + segment.duration - 0.05)) continue;
          this.videoSegments.delete(segment.number);
        }
        let request = this.segmentRequests.get(segment.number);
        if (request?.controller.signal.aborted) request = null;
        if (!request) {
          const controller = new AbortController();
          request = { controller, segment };
          request.promise = this.fetch(`${this.assetBase}/video/${segment.number}`, true,
            { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(120000)]), priority: ahead ? "low" : "high" })
            .then(async bytes => {
              if (controller.signal.aborted) return;
              await this.append(bytes); this.videoSegments.add(segment.number);
            }).finally(() => {
              if (this.segmentRequests.get(segment.number) === request) this.segmentRequests.delete(segment.number);
            });
          this.segmentRequests.set(segment.number, request);
        }
        try { await request.promise; }
        catch (error) { if (revision !== this.revision || error.name === "AbortError") return; throw error; }
      }
      if (revision === this.revision && this.videoSegments.has(segments[segments.length - 1].number)) {
        const operation = this.bufferQueue.catch(() => {}).then(() => {
          if (this.mediaSource.readyState === "open" && revision === this.revision) this.mediaSource.endOfStream();
        });
        this.bufferQueue = operation; await operation;
      }
    }

    chunkAt(position) { return Math.floor(position / this.manifest.chunkSeconds); }
    audioChunk(chunk, rate) {
      const key = `${rate}:${chunk}`;
      if (!this.audio.has(key)) {
        const request = this.fetch(`${this.assetBase}/audio/${chunk}?rate=${rate}`)
          .then(bytes => this.decoder.decodeAudioData(bytes));
        this.audio.set(key, request);
        request.catch(() => { if (this.audio.get(key) === request) this.audio.delete(key); });
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

    ensureContext() {
      if (!this.context || this.context.state === "closed") {
        const Context = window.AudioContext || window.webkitAudioContext;
        this.context = new Context({ sampleRate: 48000, latencyHint: "interactive" });
        this.gain = this.context.createGain();
        this.gain.gain.value = this.muted ? 0 : this.volume;
        this.gain.connect(this.context.destination);
      }
      return this.context;
    }

    stopAudio() {
      for (const source of this.sources) { source.stop(); source.disconnect(); }
      this.sources = []; this.audioEpoch = null;
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
      if (Math.abs(this.video.currentTime - position) > 0.005 || this.video.seeking) {
        await event(this.video, "seeked", () => { this.video.currentTime = position; });
      }
    }

    play() {
      document.getElementById("precise-retry").hidden = true;
      this.wanted = true;
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
      // Cached pause/resume should not flash a loading message. Show it only
      // when this operation really takes long enough to need feedback.
      const loading = setTimeout(() => {
        if (revision === this.revision && this.wanted) this.setStatus("Buffering…");
      }, 250);
      try {
        await this.ready;
        await timeout(authorization || this.context.resume(), "Could not start audio.");
        if (!this.wanted || revision !== this.revision) return;
        const position = this.seekPending ? Math.min(this.pendingTime, Math.max(0, this.manifest.duration - 0.05)) :
          this.video.ended ? 0 : this.video.currentTime;
        this.video.pause(); this.stopAudio();
        await Promise.all([this.ensureVideo(position), this.ensureAudio(position, this.rate)]);
        if (!this.wanted || revision !== this.revision) return;
        await this.moveVideo(position);
        if (!this.wanted || revision !== this.revision) return;
        this.seekPending = false;
        this.video.playbackRate = this.rate;
        await this.startAudio(position, revision);
        if (!this.wanted || revision !== this.revision) return;
        await timeout(this.video.play(), "Could not start video.");
        if (!this.wanted || revision !== this.revision) {
          if (!this.wanted) this.video.pause();
          return;
        }
        this.setStatus(""); this.prefetch();
      } finally { clearTimeout(loading); if (revision === this.revision) this.starting = false; this.updateControls(); }
    }

    pause() {
      this.wanted = false; this.revision++; this.starting = false;
      this.video.pause(); this.stopAudio(); this.setStatus(""); this.updateControls();
    }

    get currentTime() {
      return this.seekPending ? Math.min(this.pendingTime, this.manifest?.duration ?? Infinity) : this.video.currentTime;
    }
    set currentTime(value) {
      this.pendingTime = Math.max(0, Number(value) || 0);
      this.seekPending = true;
      const revision = ++this.revision;
      this.starting = false;
      this.cancelVideoRequests(this.pendingTime);
      this.video.pause(); this.stopAudio(); this.updateControls();
      this.seek(this.pendingTime, revision).catch(error => { if (revision === this.revision) this.fail(error); });
    }
    async seek(position, revision) {
      const loading = setTimeout(() => { if (revision === this.revision) this.setStatus("Buffering…"); }, 250);
      try {
        await this.ready;
        position = Math.min(position, Math.max(0, this.manifest.duration - 0.05));
        if (revision !== this.revision) return;
        await Promise.all([this.ensureVideo(position), this.ensureAudio(position, this.rate)]);
        if (revision !== this.revision) return;
        await this.moveVideo(position);
        if (revision !== this.revision) return;
        this.seekPending = false;
        this.setStatus("");
        if (this.wanted) await this.resume(revision);
        this.updateControls(); this.prefetch();
      } finally { clearTimeout(loading); }
    }
    get playbackRate() { return this.rate; }
    set playbackRate(value) {
      if (![0.5, 0.75, 1, 1.25, 1.5, 1.75, 2].includes(Number(value)) || Number(value) === this.rate) return;
      this.rate = Number(value);
      this.video.playbackRate = this.rate;
      if (this.wanted) {
        this.video.pause(); this.stopAudio();
        const revision = ++this.revision;
        this.resume(revision).catch(error => { if (revision === this.revision) this.fail(error); });
      }
      this.updateControls();
    }

    recover() {
      this.video.pause(); this.stopAudio();
      const revision = ++this.revision;
      this.resume(revision).catch(error => { if (revision === this.revision) this.fail(error); });
    }

    prefetch() {
      if (!this.initialized || this.closed || this.seekPending || this.scrubbing) return;
      const position = this.currentTime, index = this.chunkAt(position);
      for (const key of this.audio.keys()) {
        const chunk = Number(key.split(":")[1]);
        const rate = Number(key.split(":")[0]);
        if (chunk < index - 1 || chunk > index + 2 || ![this.rate, 2].includes(rate)) this.audio.delete(key);
      }
      if (this.manifest.audio) {
        for (const rate of new Set([this.rate, 2])) this.ensureAudio(position, rate, 1).catch(() => {});
      }
      const revision = this.revision;
      this.ensureVideo(position, 2).catch(error => { if (this.wanted && revision === this.revision) this.fail(error); });
    }

    async trimVideo() {
      if (this.trimming || !this.buffer || this.buffer.updating) return;
      const position = this.video.currentTime;
      const segments = this.manifest.segments;
      const before = Math.max(0, ...segments.filter(s => s.start + s.duration < position - 20).map(s => s.start + s.duration));
      const after = segments.find(s => s.start > position + 40)?.start ?? this.manifest.duration;
      const ranges = [];
      if (before > 0 && this.buffer.buffered.length && this.buffer.buffered.start(0) < before - 8) ranges.push([0, before]);
      const end = this.buffer.buffered.length ? this.buffer.buffered.end(this.buffer.buffered.length - 1) : 0;
      if (end > after + 8) ranges.push([after, end]);
      if (!ranges.length) return;
      this.trimming = true;
      try {
        const operation = this.bufferQueue.catch(() => {}).then(async () => {
          for (const [start, end] of ranges) await event(this.buffer, "updateend", () => this.buffer.remove(start, end));
          for (const segment of this.manifest.segments) {
            if (segment.start + segment.duration <= before || segment.start >= after) this.videoSegments.delete(segment.number);
          }
        });
        this.bufferQueue = operation; await operation;
      } finally { this.trimming = false; }
    }

    maintain() {
      this.updateControls();
      if (!this.initialized || this.closed || this.seekPending || this.scrubbing) return;
      if (this.wanted && !this.starting && !this.video.paused) {
        if (this.audioEpoch) {
          const audible = this.audioEpoch.position + Math.max(0, this.outputTime() - this.audioEpoch.time) * this.rate;
          const error = audible - this.video.currentTime;
          // Correct clock drift through gentle video pacing, without seeking
          // either stream forward and losing speech at resume.
          const correction = Math.abs(error) < 0.015 ? 0 : Math.max(-0.03, Math.min(0.03, error * 0.2));
          this.video.playbackRate = this.rate * (1 + correction);
          if (!this.scheduling) {
            const revision = this.revision;
            const task = this.scheduleAudio(revision)
              .catch(error => { if (revision === this.revision) this.fail(error); })
              .finally(() => { if (this.scheduling === task) this.scheduling = null; });
            this.scheduling = task;
          }
        }
        if (!this.prefetching) {
          this.prefetching = true;
          const revision = this.revision;
          Promise.all([this.ensureVideo(this.video.currentTime, 2), this.ensureAudio(this.video.currentTime, this.rate, 1)])
            .catch(error => { if (revision === this.revision) this.fail(error); }).finally(() => { this.prefetching = false; });
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
      this.pause(); this.setStatus(error.message || "Playback failed. Please retry.", true);
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
      this.volume = 1; this.muted = false;
      document.getElementById("precise-play").addEventListener("click", () => { this.wanted ? this.pause() : this.play(); });
      document.getElementById("precise-retry").addEventListener("click", () => location.reload());
      this.video.addEventListener("click", () => { this.wanted ? this.pause() : this.play(); });
      const seek = document.getElementById("precise-seek");
      this.bindRange(seek, {
        start: () => {
          if (this.scrubbing) return;
          this.scrubWasPlaying = this.wanted; this.scrubbing = true;
          this.pause(); this.cancelVideoRequests(-1);
        },
        input: () => this.updateControls(),
        end: () => {
          if (!this.scrubbing) return;
          this.scrubbing = false; this.currentTime = seek.value;
          if (this.scrubWasPlaying) this.play();
        },
      });
      for (const [id, offset] of [["precise-backward", -10], ["precise-forward", 10]]) {
        document.getElementById(id).addEventListener("click", () => {
          this.currentTime = Math.max(0, Math.min(this.manifest.duration - 0.05, this.currentTime + offset));
        });
      }
      document.getElementById("precise-rate").addEventListener("change", e => { this.playbackRate = e.target.value; });
      this.bindRange(document.getElementById("precise-volume"), {
        input: () => {
          this.volume = Number(document.getElementById("precise-volume").value);
          this.muted = false; this.updateVolume();
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
