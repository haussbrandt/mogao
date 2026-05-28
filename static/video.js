const video = document.getElementById("video");
const overlay = document.getElementById("subtitle-overlay");
const popup = document.getElementById("def-popup");
const popBody = document.getElementById("pop-body");
const deckWords = new Set(window.MOGAO_CONFIG.deckWords);

const bookContent = overlay; // FIXME: HACK to make textprocessor.js work
window.MOGAO_CONFIG.bookId = window.MOGAO_CONFIG.videoID; // FIXME: Yet another hack
function initStatsButton(bookContent) {} // FIXME: Another HACK to make dictionary.js work

let segmentedSubs = [];
let currentHighlightSpans = [];
let currentSentence = "";

function timeToSeconds(t) {
  const [h, m, s] = t.replace(",", ".").split(":");
  return +h * 3600 + +m * 60 + +s;
}

function parseSRT(text) {
  return text
    .trim()
    .split(/\n\n+/)
    .map((block) => {
      const lines = block.split("\n");
      const [start, end] = lines[1].split(" --> ").map(timeToSeconds);
      const text = lines
        .slice(2)
        .join(" ")
        .replace(/<[^>]+>/g, "");
      return { start, end, text };
    });
}

function renderSubtitle(sub) {
  overlay.innerHTML = `<span>${sub.text}</span>`;
}

let lastSubIndex = -1;

function updateSubtitle() {
  const t = video.currentTime;
  const idx = segmentedSubs.findIndex((s) => t >= s.start && t <= s.end);
  if (idx === lastSubIndex) return;
  lastSubIndex = idx;
  idx === -1 ? (overlay.innerHTML = "") : renderSubtitle(segmentedSubs[idx]);
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
  fetch(`/video/${window.MOGAO_CONFIG.videoId}/subtitles`)
    .then((r) => {
      if (!r.ok) throw new Error("Subtitle fetch failed: ${r.status}");
      return r.text();
    })
    .then((text) => {
      segmentedSubs = parseSRT(text);
      lastSubIndex = -1;
    })
    .catch((err) => console.error("Could not load subtitles:", err));
}

function closePopup() {
  resetUI({ keepSpacer: false });
  overlay.classList.remove("popup-open");
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
  if (!keepSpacer) overlay.normalize();
}

document.getElementById("close-popup").addEventListener("click", closePopup);

overlay.addEventListener("click", async function (e) {
  if (!localDict) return;
  video.pause();

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
  popBody.innerHTML =
    '<div style="padding:30px;text-align:center;color:#999;">Searching...</div>';
  popup.classList.add("visible");
  overlay.classList.add("popup-open");

  const results = performLookup(textChunk);
  popBody.innerHTML = "";

  if (results && results.length > 0) {
    highlightRange(startNode, startOffset, results[0].length);
    results.forEach((item) => {
      const div = document.createElement("div");
      div.className = "result-item";
      let html = "";
      item.entries.forEach((entry) => {
        const isLlm = item.llm ?? false;
        const freqHtml = isLlm
          ? `<span class="freq-badge llm-badge">LLM</span>`
          : item.frequency
            ? `<span class="freq-badge">#${item.frequency}</span>`
            : "";
        const encodedDefs = btoa(
          unescape(
            encodeURIComponent(
              `<ul>${entry.definitions.map((d) => `<li>${d}</li>`).join("")}</ul>`,
            ),
          ),
        );
        const stringifiedSubsSegment = JSON.stringify(
          segmentedSubs[lastSubIndex],
        ).replace(/"/g, "&quot;");
        const ankiBtn = `<button class="anki-btn"
              ${deckWords.has(item.word) ? "disabled" : ""}
              data-word="${item.word}"
              data-pinyin="${entry.pinyin}"
              data-defs="${encodedDefs}"
			  data-segmentedsubs="${stringifiedSubsSegment}"
              onclick="addToAnkiFromVideo(this, '${window.MOGAO_CONFIG.videoId}')">
              ${deckWords.has(item.word) ? "✓" : "+"}
            </button>`;
        html += `<div class="result-head">
              <span class="word-main">${item.word}</span>${freqHtml}${ankiBtn}
            </div>
            <div class="entry-block">
              <div class="entry-pinyin">${entry.pinyin}</div>
              <ul class="entry-defs">${entry.definitions.map((d) => `<li>${d}</li>`).join("")}</ul>
            </div>`;
      });
      div.innerHTML = html;
      popBody.appendChild(div);
    });
  } else {
    popBody.innerHTML =
      '<div style="padding:30px;text-align:center;color:#999;">No definition found.</div>';
  }
});

document.addEventListener("click", (e) => {
  if (!popup.contains(e.target) && !overlay.contains(e.target)) {
    closePopup();
  }
});

const subInput = document.getElementById("sub-input");
if (subInput) {
  subInput.addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;

    const status = document.getElementById("sub-upload-status");
    status.textContent = "Uploading…";

    const form = new FormData();
    form.append("file", file);

    try {
      const res = await fetch(
        `/video/upload-subtitles/${window.MOGAO_CONFIG.videoId}`,
        {
          method: "POST",
          body: form,
        },
      );
      if (!res.ok) throw new Error(`Server error: ${res.status}`);
      status.textContent = "Done! Reloading…";
      location.reload();
    } catch (err) {
      status.textContent = `Upload failed: ${err.message}`;
      console.error(err);
    }
  });
}
