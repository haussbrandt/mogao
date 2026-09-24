const video = document.getElementById("video");
const overlay = document.getElementById("subtitle-overlay");
const popup = document.getElementById("def-popup");
const popBody = document.getElementById("pop-body");
const modeToggle = document.getElementById("subtitle-mode-toggle");
const transcriptPanel = document.getElementById("transcript-panel");
const transcriptLines = document.getElementById("transcript-lines");
const deckWords = new Set(window.MOGAO_CONFIG.deckWords);
const progressSaveInterval = 5000;
let lastProgressSave = 0;

function saveVideoProgress(keepalive = false) {
  if (video.readyState < HTMLMediaElement.HAVE_METADATA) return;
  const secondsSinceStart = video.currentTime;
  if (!Number.isFinite(secondsSinceStart)) return;

  lastProgressSave = Date.now();
  fetch(`${window.MOGAO_CONFIG.videoBasePath}/api/save-progress`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Mogao-Request": "1" },
    body: JSON.stringify({
      video_id: window.MOGAO_CONFIG.videoId,
      seconds_since_start: secondsSinceStart,
    }),
    keepalive,
  }).catch((error) => console.error("Failed to save video progress:", error));
}

function restoreVideoProgress() {
  const savedTime = Number(window.MOGAO_CONFIG.initialPlaybackTime);
  if (!Number.isFinite(savedTime) || savedTime <= 0) return;
  video.currentTime = Number.isFinite(video.duration)
    ? Math.min(savedTime, video.duration)
    : savedTime;
}

if (video.readyState >= HTMLMediaElement.HAVE_METADATA) {
  restoreVideoProgress();
  saveVideoProgress();
} else {
  video.addEventListener(
    "loadedmetadata",
    () => {
      restoreVideoProgress();
      saveVideoProgress();
    },
    { once: true },
  );
}

video.addEventListener("timeupdate", () => {
  if (Date.now() - lastProgressSave >= progressSaveInterval) {
    saveVideoProgress();
  }
});
video.addEventListener("pause", () => saveVideoProgress());
video.addEventListener("seeked", () => saveVideoProgress());
window.addEventListener("pagehide", () => saveVideoProgress(true));

let segmentedSubs = [];
let currentHighlightSpans = [];
let currentSentence = "";
let selectedSubIndex = -1;
let activeLookupRoot = overlay;

const dictionaryPopupRenderer = createDictionaryPopupRenderer(popBody, {
  createEntryAction({ item, entry, definitionsHTML }) {
    if (!window.MOGAO_CONFIG.ankiEnabled) return null;

    const button = document.createElement("button");
    button.type = "button";
    button.className = "anki-btn";
    button.disabled = deckWords.has(item.word);
    button.dataset.word = item.word;
    button.dataset.pinyin = entry.pinyin;
    button.dataset.defs = encodeBase64Utf8(definitionsHTML);
    button.dataset.segmentedsubs = JSON.stringify(
      segmentedSubs[selectedSubIndex] ?? {
        text: currentSentence,
        start: video.currentTime,
        end: video.currentTime,
      },
    );
    button.textContent = button.disabled ? "✓" : "+";
    button.setAttribute("aria-label", `Add ${item.word} to Anki`);
    button.addEventListener("click", () => {
      window.addToAnkiFromVideo(button, window.MOGAO_CONFIG.videoId);
    });
    return button;
  },
});

function timeToSeconds(t) {
  const [h, m, s] = t.replace(",", ".").split(":");
  return +h * 3600 + +m * 60 + +s;
}

function parseSRT(text) {
  return text
    .replace(/\r\n?/g, "\n")
    .trim()
    .split(/\n\n+/)
    .map((block) => {
      const lines = block.split("\n");
      if (lines.length < 3 || !lines[1].includes(" --> ")) return null;
      const [start, end] = lines[1].split(" --> ").map(timeToSeconds);
      const text = lines
        .slice(2)
        .join(" ")
        .replace(/<[^>]+>/g, "");
      return { start, end, text };
    })
    .filter(Boolean);
}

function renderSubtitle(sub) {
  overlay.innerHTML = "";
  const span = document.createElement("span");
  span.className = "subtitle-text";
  span.textContent = sub.text;
  overlay.appendChild(span);
}

function formatTimestamp(seconds) {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = Math.floor(seconds % 60);
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`
    : `${minutes}:${String(secs).padStart(2, "0")}`;
}

function renderTranscript() {
  if (!transcriptLines) return;
  transcriptLines.innerHTML = "";
  segmentedSubs.forEach((sub, index) => {
    const line = document.createElement("div");
    line.className = "transcript-line";
    line.dataset.subIndex = index;

    const time = document.createElement("button");
    time.type = "button";
    time.className = "transcript-time";
    time.textContent = formatTimestamp(sub.start);
    time.setAttribute("aria-label", `Seek to ${time.textContent}`);

    const text = document.createElement("span");
    text.className = "transcript-text";
    text.dataset.lookupRoot = "";
    text.textContent = sub.text;

    line.append(time, text);
    transcriptLines.appendChild(line);
  });
}

function scrollTranscriptLineIntoPosition(
  line,
  behavior = "smooth",
  visibleBottomOverride,
) {
  if (!line || !transcriptLines || transcriptPanel.hidden) return;

  const linesRect = transcriptLines.getBoundingClientRect();
  const visibleBottom =
    visibleBottomOverride ??
    (popup.classList.contains("visible")
      ? window.innerHeight - popup.offsetHeight
      : linesRect.bottom);
  const visibleHeight = Math.max(0, visibleBottom - linesRect.top);
  const targetTop = linesRect.top + visibleHeight * 0.45;
  const lineTop = line.getBoundingClientRect().top;
  const scrollDistance = lineTop - targetTop;

  if (behavior === "instant") {
    transcriptLines.scrollTop += scrollDistance;
  } else {
    transcriptLines.scrollBy({ top: scrollDistance, behavior });
  }
}

function updateActiveTranscriptLine(idx) {
  if (!transcriptLines) return;
  const previousLine = transcriptLines.querySelector(".transcript-line.active");
  previousLine?.classList.remove("active");
  previousLine?.removeAttribute("aria-current");
  if (idx === -1) return;
  const activeLine = transcriptLines.querySelector(`[data-sub-index="${idx}"]`);
  activeLine?.classList.add("active");
  activeLine?.setAttribute("aria-current", "true");
  scrollTranscriptLineIntoPosition(activeLine);
}

let lastSubIndex = -1;

function updateSubtitle() {
  const t = video.currentTime;
  const idx = segmentedSubs.findIndex((s) => t >= s.start && t <= s.end);
  if (idx === lastSubIndex) return;
  lastSubIndex = idx;
  idx === -1 ? (overlay.innerHTML = "") : renderSubtitle(segmentedSubs[idx]);
  updateActiveTranscriptLine(idx);
}

function tick() {
  updateSubtitle();
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);
video.addEventListener("seeking", updateSubtitle);
video.addEventListener("seeked", updateSubtitle);
video.addEventListener("play", updateSubtitle);

if (window.MOGAO_CONFIG.hasSubtitles) {
  fetch(
    `${window.MOGAO_CONFIG.videoBasePath}/${window.MOGAO_CONFIG.videoId}/subtitles`,
  )
    .then((r) => {
      if (!r.ok) throw new Error(`Subtitle fetch failed: ${r.status}`);
      return r.text();
    })
    .then((text) => {
      segmentedSubs = parseSRT(text);
      renderTranscript();
      lastSubIndex = -1;
      updateSubtitle();
    })
    .catch((err) => console.error("Could not load subtitles:", err));
}

function closePopup() {
  dictionaryPopupRenderer.reset();
  resetUI({ keepSpacer: false });
  overlay.classList.remove("popup-open");
  const transcriptWasPopupOpen =
    transcriptPanel?.classList.contains("popup-open");
  transcriptPanel?.classList.remove("popup-open");
  const activeLine = transcriptLines?.querySelector(".transcript-line.active");
  if (transcriptWasPopupOpen) {
    scrollTranscriptLineIntoPosition(activeLine, "instant", window.innerHeight);
  }
}

function resetUI({ keepSpacer } = {}) {
  if (!keepSpacer) {
    popup.classList.remove("visible");
  }
  currentHighlightSpans.forEach((span) => {
    const parent = span.parentNode;
    if (parent) {
      while (span.firstChild) parent.insertBefore(span.firstChild, span);
      parent.removeChild(span);
    }
  });
  currentHighlightSpans = [];
  if (!keepSpacer) activeLookupRoot.normalize();
}

document.getElementById("close-popup").addEventListener("click", closePopup);

function setTranscriptMode(enabled) {
  if (!transcriptPanel || !modeToggle) return;
  closePopup();
  transcriptPanel.hidden = !enabled;
  overlay.hidden = enabled;
  modeToggle.textContent = enabled ? "Subtitles" : "Transcript";
  modeToggle.setAttribute("aria-pressed", String(enabled));
  if (enabled) updateActiveTranscriptLine(lastSubIndex);
}

modeToggle?.addEventListener("click", () => {
  setTranscriptMode(transcriptPanel.hidden);
});

const subtitleMenu = document.getElementById("subtitle-menu-container");
const replaceSubtitles = document.getElementById("replace-subtitles");
const removeSubtitles = document.getElementById("remove-subtitles");
const subInput = document.getElementById("sub-input");

function setSubtitleActionsDisabled(disabled) {
  for (const control of [subInput, replaceSubtitles, removeSubtitles]) {
    if (control) control.disabled = disabled;
  }
}

async function submitSubtitleChange(action, body) {
  const response = await fetch(
    `${window.MOGAO_CONFIG.videoBasePath}/${action}-subtitles/${window.MOGAO_CONFIG.videoId}`,
    { method: "POST", headers: { "X-Mogao-Request": "1" }, body },
  );
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(
      typeof data?.detail === "string"
        ? data.detail
        : `Server error: ${response.status}`,
    );
  }
}

document.addEventListener("click", (event) => {
  if (subtitleMenu && !subtitleMenu.contains(event.target)) {
    subtitleMenu.open = false;
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && subtitleMenu?.open) {
    subtitleMenu.open = false;
    document.getElementById("subtitle-menu-toggle").focus();
  }
});

replaceSubtitles?.addEventListener("click", () => {
  subtitleMenu.open = false;
  subInput.click();
});

removeSubtitles?.addEventListener("click", async () => {
  subtitleMenu.open = false;
  if (!confirm("Remove the subtitles from this video?")) return;
  setSubtitleActionsDisabled(true);
  try {
    await submitSubtitleChange("remove");
    location.reload();
  } catch (error) {
    setSubtitleActionsDisabled(false);
    alert(`Could not remove subtitles: ${error.message}`);
    console.error(error);
  }
});

transcriptLines?.addEventListener("click", (e) => {
  const time = e.target.closest(".transcript-time");
  if (!time) return;
  const line = time.closest(".transcript-line");
  video.currentTime = segmentedSubs[Number(line.dataset.subIndex)].start;
  video.play();
});

function lookupSubtitle(e) {
  const subtitleText = e.target.closest(".subtitle-text, .transcript-text");
  if (!subtitleText || !localDict) return;
  video.pause();
  const transcriptLine = subtitleText.closest(".transcript-line");
  selectedSubIndex = transcriptLine
    ? Number(transcriptLine.dataset.subIndex)
    : lastSubIndex;
  activeLookupRoot = subtitleText.closest("[data-lookup-root]") || overlay;

  let range,
    x = e.clientX,
    y = e.clientY;
  if (document.caretRangeFromPoint) {
    range = document.caretRangeFromPoint(x, y);
  } else if (document.caretPositionFromPoint) {
    const pos = document.caretPositionFromPoint(x, y);
    range = document.createRange();
    range.setStart(pos.offsetNode, pos.offset);
    range.collapse(true);
  }

  let hasChinese = false,
    textChunk = "",
    startNode,
    startOffset;

  if (range && range.startContainer.nodeType === Node.TEXT_NODE) {
    startNode = range.startContainer;
    startOffset = range.startOffset;

    if (startOffset > 0) {
      const tempRange = document.createRange();
      tempRange.setStart(startNode, startOffset - 1);
      tempRange.setEnd(startNode, startOffset);
      for (const rect of tempRange.getClientRects()) {
        if (
          x >= rect.left &&
          x <= rect.right &&
          y >= rect.top &&
          y <= rect.bottom
        ) {
          startOffset -= 1;
          break;
        }
      }
    }

    textChunk = getSmartSnippet(startNode, startOffset);
    const cleanChunk = textChunk.replace(/\s/g, "");
    if (cleanChunk && cleanChunk[0].match(/[\u4e00-\u9fff]/)) {
      hasChinese = true;
      currentSentence = getSentence(startNode, startOffset);
    }
  }

  if (!hasChinese) {
    closePopup();
    return;
  }

  resetUI({ keepSpacer: true });
  popBody.replaceChildren();
  const searching = document.createElement("div");
  searching.className = "dictionary-message";
  searching.textContent = "Searching...";
  popBody.appendChild(searching);
  popup.classList.add("visible");
  overlay.classList.add("popup-open");
  transcriptPanel?.classList.add("popup-open");
  scrollTranscriptLineIntoPosition(transcriptLine, "auto");

  const results = performLookup(textChunk);

  if (results.length > 0) {
    highlightRange(startNode, startOffset, results[0].length);
  }
  dictionaryPopupRenderer.show(results);
}

overlay.addEventListener("click", lookupSubtitle);
transcriptLines?.addEventListener("click", lookupSubtitle);

document.addEventListener("click", (e) => {
  if (
    !popup.contains(e.target) &&
    !overlay.contains(e.target) &&
    !transcriptLines?.contains(e.target)
  ) {
    closePopup();
  }
});

if (subInput) {
  subInput.addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    setSubtitleActionsDisabled(true);

    const status = document.getElementById("sub-upload-status");
    if (status) status.textContent = "Uploading…";
    if (replaceSubtitles) {
      replaceSubtitles.textContent = "Uploading…";
    }

    const form = new FormData();
    form.append("file", file);

    try {
      await submitSubtitleChange("upload", form);
      if (status) status.textContent = "Done! Reloading…";
      location.reload();
    } catch (err) {
      setSubtitleActionsDisabled(false);
      if (status) status.textContent = `Upload failed: ${err.message}`;
      if (replaceSubtitles) {
        replaceSubtitles.textContent = "Replace subtitles";
        alert(`Could not replace subtitles: ${err.message}`);
      }
      console.error(err);
    } finally {
      subInput.value = "";
    }
  });
}
