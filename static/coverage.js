document.addEventListener("DOMContentLoaded", () => {
  const grid = document.querySelector("[data-coverage-kind]");
  const dialog = document.getElementById("coverage-dialog");
  if (!grid || !dialog || !window.LocalVocabulary) return;
  const kind = grid.dataset.coverageKind;
  const buttons = [...grid.querySelectorAll("[data-coverage-id]")];
  const message = document.getElementById("coverage-message");
  const results = document.getElementById("coverage-results");
  const chart = document.getElementById("coverage-chart");
  const position = document.getElementById("coverage-position");
  const lookup = document.getElementById("coverage-lookup");
  const lookupBody = document.getElementById("coverage-lookup-body");
  let lookupRenderer = null;
  let lookupRequest = 0;
  let itemDictionary = null;
  const statuses = {
    analysing: "Analysing vocabulary…",
    pending: "Waiting for dictionary processing. Reload the page when it finishes.",
    dictionary_error: "Dictionary processing needs a retry on the Status page.",
    no_subtitles: "Add subtitles to calculate vocabulary coverage.",
    empty: "No Chinese words found to analyse.",
    missing: "This item is no longer available.",
    error: "Vocabulary analysis failed. Try reloading the page.",
  };
  let selectedButton = null;
  let data = null;
  let requestNumber = 0;
  let selectionLine = null;
  let chartWidth = 640;
  const details = new Map();
  let snapshot = null;
  let localRevision = null;
  let libraryReady = Promise.resolve();
  let libraryRequest = 0;
  let libraryItems = {};
  let analysisPoll = null;
  const cacheKey = `mogao:coverage:${kind}`;
  let showAll = false;
  let wholeItem = false;
  let wholeItemWords = [];
  const showAllButton = document.getElementById("coverage-show-all");
  const sectionWordsButton = document.getElementById("coverage-section-words");
  const wholeItemWordsButton = document.getElementById("coverage-entire-words");
  wholeItemWordsButton.textContent = kind === "books" ? "Entire book" : "Entire video";
  const number = (value) =>
    value.toLocaleString(undefined, { maximumFractionDigits: 1 });

  async function fetchJson(url) {
    const response = await fetch(url, { cache: "no-store" });
    const body = await response.json();
    if (!response.ok) {
      const error = new Error(
        body.detail || "Could not load vocabulary coverage.",
      );
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function updateButton(button, score) {
    const card = button.closest("[data-sort-card]");
    card.dataset.coverage = score.status === "ready" ? score.coverage : "";
    button.textContent =
      score.status === "ready"
        ? `${number(score.coverage)}% known`
        : {
            pending: "Analysing…",
            analysing: "Analysing…",
            dictionary_error: "Dictionary incomplete",
            no_subtitles: "No subtitles",
            empty: "No Chinese text",
          }[score.status] || "Coverage unavailable";
    button.title = "Vocabulary coverage";
    button.setAttribute("aria-label", `${button.textContent} · Open vocabulary coverage`);
  }

  function showScores(items, cached = false) {
    for (const button of buttons) {
      const score = items[button.dataset.coverageId];
      if (cached && !score) continue;
      updateButton(button, score || { status: "missing" });
      if (cached) button.title = "Saved coverage · Updating…";
    }
    grid.dispatchEvent(new Event("coverage:updated"));
  }

  function loadItemDictionary() {
    if (!itemDictionary) {
      const pending = Promise.all([
        loadDictionary(),
        fetchJson(selectedButton.dataset.dictionaryUrl),
      ]);
      itemDictionary = pending;
      // A failed preload can be retried on lookup. An old item's failure must
      // not discard the dictionary of a newly opened item.
      void pending.catch(() => {
        if (itemDictionary === pending) itemDictionary = null;
      });
    }
    return itemDictionary;
  }

  async function openLookup(word) {
    const request = ++lookupRequest;
    if (!lookup.open) lookup.showModal();
    lookupBody.replaceChildren();
    const loading = document.createElement("div");
    loading.className = "dictionary-message";
    loading.textContent = "Loading…";
    lookupBody.appendChild(loading);
    try {
      const [, generated] = await loadItemDictionary();
      if (request !== lookupRequest) return;
      replaceGeneratedDictionaryWords(generated.words);
      lookupRenderer ||= createDictionaryPopupRenderer(lookupBody);
      const result = dictionaryResultForWord(word);
      // Keep the known-word action available even for dictionary misses.
      lookupRenderer.show([result || {
        word, entries: [{ pinyin: "", definitions: ["No definition found."] }],
      }]);
    } catch (error) {
      if (request !== lookupRequest) return;
      loading.textContent = "Could not load dictionary. Close and try again.";
    }
  }

  function svg(tag, attributes, text) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes))
      element.setAttribute(key, value);
    if (text !== undefined) element.textContent = text;
    chart.appendChild(element);
    return element;
  }

  const x = (offset) => 48 + ((chartWidth - 66) * offset) / data.total;

  function selectWindow(index) {
    index = Math.max(0, Math.min(data.windows.length - 1, index));
    position.value = index;
    const entry = data.windows[index];
    const markers = data.markers.filter(
      (marker) => marker.start < entry.end && marker.end > entry.start,
    );
    const label =
      markers.length > 1
        ? `${markers[0].label} – ${markers[markers.length - 1].label}`
        : markers[0]?.label || "Text";
    document.getElementById("coverage-section-title").textContent = label;
    document.getElementById("coverage-section").textContent =
      `${number(entry.coverage)}% known · ${number(entry.unfamiliar)} / ${number(entry.total)} unfamiliar occurrences`;
    position.setAttribute("aria-valuetext", `${label}, words ${entry.start + 1}–${entry.end}`);
    const wordEntry = wholeItem
      ? { words: wholeItemWords, unfamiliar: data.unfamiliar }
      : entry;
    const words = document.getElementById("coverage-words");
    words.replaceChildren();
    const shownWords = showAll ? wordEntry.words : wordEntry.words.slice(0, 10);
    const shownOccurrences = shownWords.reduce(
      (sum, item) => sum + item.count,
      0,
    );
    document.getElementById("coverage-words-shown").textContent =
      `${number(shownWords.length)} / ${number(wordEntry.words.length)}`;
    document.getElementById("coverage-occurrences-shown").textContent =
      `${number(shownOccurrences)} / ${number(wordEntry.unfamiliar)}`;
    showAllButton.hidden = wordEntry.words.length <= 10;
    showAllButton.textContent = showAll ? "Show top 10" : "Show all";
    sectionWordsButton.setAttribute("aria-pressed", String(!wholeItem));
    wholeItemWordsButton.setAttribute("aria-pressed", String(wholeItem));
    if (!wordEntry.words.length)
      words.textContent = "All words known";
    for (const item of shownWords) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "coverage-word";
      chip.textContent = `${item.word} × ${item.count}`;
      chip.title = item.unrecognized ? "Look up · Unrecognized word" : "Look up";
      chip.addEventListener("click", () => void openLookup(item.word));
      words.appendChild(chip);
    }
    const coordinate = x((entry.start + entry.end) / 2);
    selectionLine.setAttribute("x1", coordinate);
    selectionLine.setAttribute("x2", coordinate);
  }

  function render() {
    chart.replaceChildren();
    chartWidth = Math.max(280, chart.clientWidth);
    chart.setAttribute("viewBox", `0 0 ${chartWidth} 250`);
    const y = (density) => 210 - (190 * density) / 100;
    for (const density of [0, 25, 50, 75, 100]) {
      svg("line", {
        x1: 48,
        x2: chartWidth - 18,
        y1: y(density),
        y2: y(density),
        stroke: "currentColor",
        opacity: 0.15,
      });
      svg(
        "text",
        { x: 40, y: y(density) + 4, "text-anchor": "end" },
        `${number(density)}%`,
      );
    }
    for (const percent of [0, 25, 50, 75, 100]) {
      svg(
        "text",
        { x: x((data.total * percent) / 100), y: 236, "text-anchor": "middle" },
        `${percent}%`,
      );
    }
    if (kind === "books" && data.markers.length <= 100) {
      for (const marker of data.markers.slice(1)) {
        svg("line", {
          x1: x(marker.start),
          x2: x(marker.start),
          y1: 20,
          y2: 210,
          stroke: "currentColor",
          opacity: 0.12,
          "stroke-dasharray": "3 4",
        });
      }
    }
    const points = data.windows.flatMap(
      (w) => [`${x(w.start)},${y(w.coverage)}`, `${x(w.end)},${y(w.coverage)}`],
    );
    svg("polyline", {
      points: points.join(" "),
      fill: "none",
      stroke: "var(--accent-interactive)",
      "stroke-width": 2.5,
    });
    selectionLine = svg("line", {
      x1: 48,
      x2: 48,
      y1: 20,
      y2: 210,
      stroke: "var(--accent-primary)",
      "stroke-width": 2,
    });
    position.max = data.windows.length - 1;
    position.disabled = data.windows.length === 1;
    document.getElementById("coverage-summary").textContent =
      `${number(data.coverage)}% known · ${number(data.unknown_words)} unfamiliar words`;
    selectWindow(0);
  }

  async function loadLibrary(fresh = false) {
    const request = ++libraryRequest;
    clearTimeout(analysisPoll);
    let result;
    try {
      result = await fetchJson(
        `/api/coverage/${kind}${fresh ? "?refresh=true" : snapshot ? `?snapshot=${snapshot}` : ""}`,
      );
    } catch (error) {
      if (error.status !== 409 || fresh || !snapshot) throw error;
      result = await fetchJson(`/api/coverage/${kind}`);
    }
    if (request !== libraryRequest) return;
    const changed = snapshot && (
      snapshot !== result.snapshot || localRevision !== result.local_revision
    );
    snapshot = result.snapshot;
    if (!window.LocalVocabulary.setWords(result.local_words, result.local_revision))
      return;
    localRevision = result.local_revision;
    const selectedId = selectedButton?.dataset.coverageId;
    const analysisFinished = libraryItems[selectedId]?.status === "analysing" &&
      result.items[selectedId] && result.items[selectedId].status !== "analysing";
    libraryItems = result.items;
    showScores(result.items);
    try {
      sessionStorage.setItem(cacheKey, JSON.stringify(result.items));
    } catch {} // Scores still work when browser storage is unavailable.
    if (changed) {
      details.clear();
      if (dialog.open) void loadDetail(true);
    } else if (dialog.open && !data && analysisFinished) {
      void loadDetail(true);
    }
    if (Object.values(result.items).some((score) => score.status === "analysing")) {
      analysisPoll = setTimeout(() => loadLibrary().catch(() => {}), 1500);
    }
    return result;
  }

  async function loadDetail(preservePosition = false) {
    const currentRequest = ++requestNumber;
    const button = selectedButton;
    const selected = preservePosition ? Number(position.value) : 0;
    if (preservePosition && data) {
      message.textContent = "";
    } else {
      results.hidden = true;
      data = null;
      message.textContent = "Loading vocabulary coverage…";
    }
    try {
      await libraryReady;
      if (currentRequest !== requestNumber) return;
      const id = button.dataset.coverageId;
      let score = details.get(id);
      if (!score) {
        const url = () => `/api/coverage/${kind}/${id}?snapshot=${snapshot}`;
        try {
          score = await fetchJson(url());
        } catch (error) {
          if (error.status !== 409) throw error;
          details.clear();
          libraryReady = loadLibrary(true);
          await libraryReady;
          score = await fetchJson(url());
        }
      }
      if (currentRequest !== requestNumber) return;
      const newerVocabulary = score.local_revision > (
        window.MOGAO_CONFIG?.localKnownRevision ?? localRevision
      );
      if (!window.LocalVocabulary.setWords(score.local_words, score.local_revision)) {
        details.delete(id);
        return loadDetail(preservePosition);
      }
      if (newerVocabulary) {
        details.clear();
        // Item details can discover words marked on another device.
        // Refresh all cards and saved scores without blocking this chart.
        void loadLibrary().catch(() => {});
      }
      updateButton(button, score);
      grid.dispatchEvent(new Event("coverage:updated"));
      if (score.status !== "ready") {
        results.hidden = true;
        data = null;
        message.textContent = statuses[score.status] || statuses.error;
        return;
      }
      data = score;
      const combined = new Map();
      for (const window of data.windows) {
        for (const item of window.words) {
          const previous = combined.get(item.word);
          if (previous) previous.count += item.count;
          else combined.set(item.word, { ...item });
        }
      }
      wholeItemWords = [...combined.values()].sort(
        (a, b) => b.count - a.count || a.word.localeCompare(b.word),
      );
      details.set(id, score);
      message.textContent = "";
      results.hidden = false;
      render();
      selectWindow(selected);
    } catch (error) {
      if (currentRequest === requestNumber) message.textContent = error.message;
    }
  }

  for (const button of buttons) {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      selectedButton = button;
      itemDictionary = null;
      showAll = false;
      wholeItem = false;
      document.getElementById("coverage-item-title").textContent =
        button.dataset.coverageTitle;
      dialog.showModal();
      void loadDetail();
      void loadItemDictionary();
    });
  }
  document
    .getElementById("coverage-close")
    .addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    requestNumber++;
    lookup.close();
  });
  document.getElementById("coverage-lookup-close")
    .addEventListener("click", () => lookup.close());
  lookup.addEventListener("close", () => {
    lookupRequest++;
    lookupRenderer?.reset();
  });
  window.addEventListener("mogao:known-words-changed", () => {
    details.clear();
    requestNumber++;
    if (dialog.open) void loadDetail(true);
    // Only loading the library's vocabulary snapshot gates the open chart.
    // Rescoring the rest of the library happens independently.
    libraryReady.then(() => loadLibrary()).catch(() => {});
  });
  showAllButton.addEventListener("click", () => {
    showAll = !showAll;
    if (data) selectWindow(Number(position.value));
  });
  for (const [button, entire] of [[sectionWordsButton, false], [wholeItemWordsButton, true]]) {
    button.addEventListener("click", () => {
      wholeItem = entire;
      showAll = false;
      if (data) selectWindow(Number(position.value));
    });
  }
  position.addEventListener("input", () => {
    if (!wholeItem) showAll = false;
    if (data) selectWindow(Number(position.value));
  });
  chart.addEventListener("click", (event) => {
    if (!data) return;
    const point = chart.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const coordinate = point.matrixTransform(chart.getScreenCTM().inverse()).x;
    const offset = Math.max(
      0,
      Math.min(
        data.total - 1,
        ((coordinate - 48) / (chartWidth - 66)) * data.total,
      ),
    );
    selectWindow(Math.floor(offset / data.window_size));
  });
  window.addEventListener("resize", () => {
    if (!data || !dialog.open) return;
    const selected = Number(position.value);
    render();
    selectWindow(selected);
  });

  if (buttons.length) {
    try {
      const saved = JSON.parse(sessionStorage.getItem(cacheKey));
      if (saved) showScores(saved, true);
    } catch {}
    libraryReady = loadLibrary();
    // Warm the shared dictionary on library entry, independently of scores.
    // Keep custom dictionaries scoped to the item whose coverage is opened.
    void loadDictionary().catch(() => {});
    libraryReady.then((result) => {
      if (result?.cached_vocabulary) {
        void loadLibrary(true).catch((error) => {
          for (const button of buttons) button.title = error.message;
        });
      }
    }).catch(() => {});
    libraryReady.catch((error) => {
      for (const button of buttons) {
        if (!button.closest("[data-sort-card]").dataset.coverage)
          button.textContent = "Coverage unavailable";
        button.title = error.message;
      }
    });
  }
});
