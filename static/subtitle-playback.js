// Subtitle timing drives these controls; gaps are not audio speech detection.
window.createSubtitlePlayback = function (playback, video, isHoldingSpeed) {
  let cues = [];
  let intervals = [];
  let previousTime = null;
  let pinnedIndex = -1;
  let navigationIndex = -1;
  let savedRate = null;
  let suppressedGap = null;
  let pendingLoopIndex = -1;
  let loopIndex = -1;
  const autopause = document.getElementById("subtitle-autopause");
  const loop = document.getElementById("subtitle-loop");
  const gapMode = document.getElementById("subtitle-gap-mode");
  const previousCue = document.getElementById("playback-previous-cue");
  const nextCue = document.getElementById("playback-next-cue");
  if (autopause) autopause.checked = autopause.getAttribute("aria-pressed") === "true";
  if (loop) loop.checked = loop.getAttribute("aria-pressed") === "true";
  playback.warmGapSpeed = gapMode?.value === "fast";
  let settingsSave = Promise.resolve();

  function navigationTargets() {
    const time = playback.currentTime;
    const selected = cues[navigationIndex];
    // Media clocks can land just below the requested cue start after a seek.
    if (playback.scrubbing || !selected || time < selected.start - 0.001 || time >= selected.end) {
      navigationIndex = -1;
    }
    // Autopause keeps the cue visible after its end; navigate relative to it.
    const active = pinnedIndex !== -1 ? pinnedIndex :
      navigationIndex !== -1 ? navigationIndex :
        cues.findIndex(cue => time >= cue.start && time < cue.end);
    if (active !== -1) return [active - 1, active + 1];
    const next = cues.findIndex(cue => cue.start > time);
    return [next === -1 ? cues.length - 1 : next - 1, next];
  }

  function updateNavigation() {
    const targets = navigationTargets();
    [previousCue, nextCue].forEach((button, direction) => {
      if (button) button.disabled = !playback.initialized ||
        targets[direction] < 0 || targets[direction] >= cues.length;
    });
  }

  [previousCue, nextCue].forEach((button, direction) => {
    button?.addEventListener("click", () => {
      const index = navigationTargets()[direction];
      const cue = cues[index];
      if (!playback.initialized || !cue) return;
      resetPosition();
      playback.currentTime = cue.start;
      // Remember the chosen cue even when an earlier cue covers this time too.
      navigationIndex = index;
      if (loop?.checked) loopIndex = index;
      updateNavigation();
    });
  });

  function saveSettings() {
    const body = JSON.stringify({
      autopause: !!autopause?.checked,
      subtitle_loop: !!loop?.checked,
      gap_mode: gapMode?.value ?? "normal",
    });
    // Snapshot each change and save in order, including rapid toggles.
    settingsSave = settingsSave.then(async () => {
      const response = await fetch(`${window.MOGAO_CONFIG.videoBasePath}/api/save-subtitle-playback`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Mogao-Request": "1" },
        body,
        keepalive: true,
      });
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    }).catch(error => console.error("Could not save subtitle playback settings", error));
  }

  function restoreRate() {
    if (savedRate === null) return;
    const rate = savedRate;
    savedRate = null;
    playback.playbackRate = rate;
  }

  function resetPosition() {
    previousTime = null;
    pendingLoopIndex = -1;
    pinnedIndex = -1;
    suppressedGap = null;
    restoreRate();
  }

  function restartCue(index) {
    resetPosition();
    playback.currentTime = cues[index].start;
    // The seek clears selection; restore it for this and subsequent repeats.
    loopIndex = index;
    navigationIndex = index;
  }

  // Controller seeks include timeline, transcript, and skip-button navigation.
  // Native seeking events also occur during rate changes and seek completion.
  video.addEventListener("playback-seek", () => {
    navigationIndex = -1;
    pendingLoopIndex = -1;
    loopIndex = -1;
  });
  video.addEventListener("seeking", () => {
    // Rate changes also align the video with the audio clock internally.
    if (playback.seekPending) resetPosition();
  });
  video.addEventListener("play", () => { pinnedIndex = -1; });
  video.addEventListener("playback-before-play", () => {
    if (loop?.checked && pendingLoopIndex !== -1) restartCue(pendingLoopIndex);
  });
  video.addEventListener("playback-ended", (event) => {
    if (!loop?.checked || !playback.wanted || playback.seekPending || playback.scrubbing) return;
    const position = previousTime ?? playback.currentTime;
    const index = loopIndex !== -1 ? loopIndex : navigationIndex !== -1 ? navigationIndex :
      cues.findIndex(cue => position >= cue.start - 0.001 && position < cue.end);
    if (index === -1) return;
    event.preventDefault();
    if (autopause?.checked) {
      loopIndex = index;
      pendingLoopIndex = index;
      pinnedIndex = index;
      previousTime = playback.currentTime;
      playback.pause();
      restoreRate();
    } else {
      restartCue(index);
    }
  });
  video.addEventListener("pause", () => {
    if (!playback.wanted) restoreRate();
  });
  video.addEventListener("playback-speed-selection", () => {
    // An explicit speed choice wins until this gap ends.
    suppressedGap = intervals.find(interval => playback.currentTime < interval.start);
    restoreRate();
  });
  gapMode?.addEventListener("change", () => {
    playback.warmGapSpeed = gapMode.value === "fast";
    suppressedGap = null;
    restoreRate();
    saveSettings();
  });
  autopause?.addEventListener("click", () => {
    autopause.checked = !autopause.checked;
    autopause.setAttribute("aria-pressed", String(autopause.checked));
    autopause.textContent = `Autopause: ${autopause.checked ? "On" : "Off"}`;
    autopause.dispatchEvent(new Event("change"));
  });
  autopause?.addEventListener("change", () => {
    previousTime = playback.currentTime;
    if (!autopause.checked) pinnedIndex = -1;
    saveSettings();
  });
  loop?.addEventListener("click", () => {
    loop.checked = !loop.checked;
    loop.setAttribute("aria-pressed", String(loop.checked));
    loop.textContent = `Loop: ${loop.checked ? "On" : "Off"}`;
    pendingLoopIndex = loop.checked ? pinnedIndex : -1;
    loopIndex = loop.checked ? (pinnedIndex !== -1 ? pinnedIndex : navigationIndex) : -1;
    saveSettings();
  });

  return {
    restoreRate,
    setCues(value) {
      resetPosition();
      navigationIndex = -1;
      loopIndex = -1;
      cues = value;
      // Merge overlapping cues so a gap can never skip another cue's speech.
      intervals = [];
      for (const cue of [...cues].sort((a, b) => a.start - b.start)) {
        const last = intervals[intervals.length - 1];
        if (last && cue.start <= last.end) last.end = Math.max(last.end, cue.end);
        else intervals.push({ start: cue.start, end: cue.end });
      }
      if (autopause) autopause.disabled = !cues.length;
      if (gapMode) gapMode.disabled = !cues.length;
      if (loop) loop.disabled = !cues.length;
      updateNavigation();
    },
    tick() {
      updateNavigation();
      const time = playback.currentTime;
      if (playback.wanted && pendingLoopIndex !== -1 && loop?.checked) {
        restartCue(pendingLoopIndex);
        return -1;
      }
      if (gapMode?.value === "fast" && !isHoldingSpeed()) playback.prepareGapSpeed?.();
      if (playback.seekPending || playback.scrubbing) {
        previousTime = null;
        pinnedIndex = -1;
        return -1;
      }
      if (!playback.wanted) {
        restoreRate();
        previousTime = time;
        return pinnedIndex;
      }
      pinnedIndex = -1;
      if (loop?.checked && loopIndex === -1) {
        // Keep one cue selected until an explicit seek or toggle releases it.
        const position = previousTime ?? time;
        loopIndex = navigationIndex !== -1 ? navigationIndex :
          cues.findIndex(cue => position >= cue.start - 0.001 && position < cue.end);
        if (loopIndex === -1) {
          loopIndex = cues.findIndex(cue => time >= cue.start - 0.001 && time < cue.end);
        }
      }
      if ((autopause?.checked || loop?.checked) && previousTime !== null && time >= previousTime) {
        let ending = -1;
        cues.forEach((cue, index) => {
          if (loop?.checked && loopIndex !== -1 && index !== loopIndex) return;
          if (cue.end > previousTime && cue.end <= time &&
              (ending === -1 || cue.end < cues[ending].end)) ending = index;
        });
        if (ending !== -1) {
          if (!autopause?.checked) {
            restartCue(ending);
            return -1;
          }
          pendingLoopIndex = loop?.checked ? ending : -1;
          pinnedIndex = ending;
          previousTime = time;
          playback.pause();
          restoreRate();
          return pinnedIndex;
        }
      }
      previousTime = time;
      const nextIndex = intervals.findIndex(interval => interval.start > time);
      const next = intervals[nextIndex];
      const end = nextIndex > 0 ? intervals[nextIndex - 1].end : 0;
      const inGap = next && next.start - end >= 1 &&
        time >= end + 0.3 && time < next.start - 0.3;
      if (!inGap || gapMode?.value === "normal" || isHoldingSpeed()) {
        restoreRate();
        if (!inGap) suppressedGap = null;
      } else if (gapMode?.value === "skip") {
        restoreRate();
        playback.currentTime = next.start - 0.3;
        previousTime = null;
      } else if (gapMode?.value === "fast" && suppressedGap !== next && savedRate === null) {
        // Keep playing normally if the alternative audio is still loading.
        if (!playback.gapSpeedReady || playback.gapSpeedReady()) {
          savedRate = playback.playbackRate;
          playback.playbackRate = 3;
        }
      }
      return -1;
    },
  };
};
