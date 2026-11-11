// Complete, revisioned datasets use the browser's private HTTP cache across visits.
// Keep word lists compact in memory too; only the displayed words need objects.
class CoverageData {
  constructor(kind) {
    this.kind = kind;
    this.revisions = new Map();
    this.details = new Map();
    this.pending = new Map();
  }

  setRevisions(items) {
    for (const [id, item] of Object.entries(items)) {
      this.revisions.set(id, item.revision);
    }
    for (const id of this.details.keys()) {
      if (!(id in items)) this.details.delete(id);
    }
  }

  peek(id) {
    const data = this.details.get(id);
    return data?.revision === this.revisions.get(id) ? data : null;
  }

  async load(id) {
    const cached = this.peek(id);
    if (cached) return cached;
    const revision = this.revisions.get(id);
    const key = `${id}:${revision || ""}`;
    if (this.pending.has(key)) return this.pending.get(key);
    const request = (async () => {
      const query = revision ? `?revision=${encodeURIComponent(revision)}` : "";
      const response = await fetch(`/api/coverage/${this.kind}/${id}${query}`);
      if (!response.ok) throw new Error("Could not load vocabulary coverage.");
      const result = await response.json();
      // A vocabulary update during the download must not restore old results.
      if (this.revisions.get(id) !== revision) return this.load(id);
      if (result.status === "ready" || result.status === "empty") {
        this.revisions.set(id, result.revision);
        this.details.set(id, result);
      }
      return result;
    })();
    this.pending.set(key, request);
    try {
      return await request;
    } finally {
      this.pending.delete(key);
    }
  }

  async preload(ids) {
    const queue = [...ids];
    const worker = async () => {
      while (queue.length) {
        const id = queue.shift();
        try {
          await this.load(id);
        } catch {} // A click or the next refresh retries failed downloads.
      }
    };
    await Promise.all([worker(), worker()]);
  }

  words(data, entry, all) {
    const pairs = entry.words;
    const limit = all ? pairs.length : Math.min(20, pairs.length);
    const words = [];
    for (let index = 0; index < limit; index += 2) {
      const [word, unrecognized] = data.vocabulary[pairs[index]];
      words.push({ word, count: pairs[index + 1], unrecognized: !!unrecognized });
    }
    return words;
  }
}

function initializeCoverage() {
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
  const coverageData = new CoverageData(kind);
  let preloading = null;
  let libraryRequest = 0;
  let analysisPoll = null;
  let showAll = false;
  let wholeItem = false;
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
      throw new Error(body.detail || "Could not load vocabulary coverage.");
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

  function showScores(items) {
    for (const button of buttons) {
      const score = items[button.dataset.coverageId];
      updateButton(button, score || { status: "analysing" });
    }
    grid.dispatchEvent(new Event("coverage:updated"));
  }

  function loadItemDictionary() {
    if (!itemDictionary) {
      const pending = fetchJson(selectedButton.dataset.dictionaryUrl);
      itemDictionary = pending;
      // A failed preload can be retried on lookup. An old item's failure must
      // not discard the dictionary of a newly opened item.
      void pending.catch(() => {
        if (itemDictionary === pending) itemDictionary = null;
      });
    }
    return itemDictionary;
  }

  async function openLookup(word, pinyin = "") {
    const request = ++lookupRequest;
    if (!lookup.open) lookup.showModal();
    lookupBody.replaceChildren();
    const loading = document.createElement("div");
    loading.className = "dictionary-message";
    loading.textContent = "Loading…";
    lookupBody.appendChild(loading);
    try {
      const [, generated] = await Promise.all([loadDictionary(), loadItemDictionary()]);
      if (request !== lookupRequest) return;
      replaceGeneratedDictionaryWords(generated.words);
      lookupRenderer ||= createDictionaryPopupRenderer(lookupBody);
      const result = dictionaryResultForWord(word, pinyin);
      // Keep the known-word action available even for dictionary misses.
      lookupRenderer.show([result || {
        word, entries: [{ pinyin: "", definitions: ["No definition found."] }],
      }]);
    } catch (error) {
      if (request !== lookupRequest) return;
      loading.textContent = "Could not load dictionary. Close and try again.";
    }
  }

  const coverageTab = document.getElementById("coverage-tab");
  const dictionaryTab = document.getElementById("custom-dictionary-tab");
  const dictionarySearch = document.getElementById("custom-dictionary-search");
  const dictionaryMessage = document.getElementById("custom-dictionary-message");
  const dictionaryEntries = document.getElementById("custom-dictionary-entries");
  const findDictionaryWords = createVocabularySearch();
  let generatedDictionary = null;
  let dictionaryRequest = 0;

  function renderDictionary() {
    dictionaryEntries.replaceChildren();
    if (!generatedDictionary) return;
    const words = generatedDictionary.words || {};
    const entries = Object.keys(words);
    const matches = findDictionaryWords(entries, dictionarySearch.value, words);
    dictionaryMessage.textContent = entries.length
      ? `${number(matches.length)} / ${number(entries.length)} generated entries`
      : "No custom dictionary entries have been generated for this item.";
    if (generatedDictionary.status === "processing") {
      dictionaryMessage.textContent += " Dictionary generation is still in progress.";
    } else if (generatedDictionary.status === "error") {
      dictionaryMessage.textContent += " Dictionary generation is incomplete. Retry on the Status page.";
    }
    const fragment = document.createDocumentFragment();
    for (const word of matches) {
      const raw = words[word];
      const article = document.createElement("article");
      article.className = "custom-dictionary-entry";
      const heading = document.createElement("h3");
      const wordButton = document.createElement("button");
      wordButton.type = "button";
      wordButton.className = "custom-dictionary-word";
      wordButton.textContent = word;
      wordButton.setAttribute("aria-label", `Look up ${word}`);
      wordButton.addEventListener("click", () => void openLookup(word));
      heading.appendChild(wordButton);
      article.appendChild(heading);
      for (const entry of raw.e || []) {
        const pronunciation = document.createElement("p");
        pronunciation.className = "custom-dictionary-pinyin";
        pronunciation.textContent = entry.p || "";
        article.appendChild(pronunciation);
        article.appendChild(createDefinitionList(entry.d || [], word));
      }
      fragment.appendChild(article);
    }
    dictionaryEntries.appendChild(fragment);
  }

  async function showDictionary() {
    const request = ++dictionaryRequest;
    dictionaryMessage.textContent = "Loading custom dictionary…";
    try {
      const [, generated] = await Promise.all([loadDictionary(), loadItemDictionary()]);
      if (request !== dictionaryRequest || !dialog.open) return;
      generatedDictionary = generated;
      replaceGeneratedDictionaryWords(generated.words);
      renderDictionary();
    } catch {
      if (request === dictionaryRequest) {
        dictionaryMessage.textContent = "Could not load custom dictionary. Switch tabs to try again.";
      }
    }
  }

  function selectTab(dictionary) {
    coverageTab.setAttribute("aria-selected", String(!dictionary));
    dictionaryTab.setAttribute("aria-selected", String(dictionary));
    coverageTab.tabIndex = dictionary ? -1 : 0;
    dictionaryTab.tabIndex = dictionary ? 0 : -1;
    document.getElementById("coverage-panel").hidden = dictionary;
    document.getElementById("custom-dictionary-panel").hidden = !dictionary;
    if (dictionary) void showDictionary();
    else if (data) {
      render();
      selectWindow(Number(position.value));
    }
  }

  for (const tab of [coverageTab, dictionaryTab]) {
    tab.addEventListener("click", () => selectTab(tab === dictionaryTab));
    tab.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === "Home" ? coverageTab : event.key === "End" ? dictionaryTab
        : tab === coverageTab ? dictionaryTab : coverageTab;
      selectTab(next === dictionaryTab);
      next.focus();
    });
  }
  dictionarySearch.addEventListener("input", renderDictionary);
  dictionaryEntries.addEventListener("click", event => {
    const reference = event.target.closest(".dictionary-reference");
    if (!reference || !dictionaryEntries.contains(reference)) return;
    event.preventDefault();
    void openLookup(reference.dataset.dictionaryReference, reference.dataset.referencePinyin);
  });

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
    const coordinate = x((entry.start + entry.end) / 2);
    selectionLine.setAttribute("x1", coordinate);
    selectionLine.setAttribute("x2", coordinate);
    const wordEntry = wholeItem ? data : entry;
    const words = document.getElementById("coverage-words");
    words.replaceChildren();
    const shownWords = coverageData.words(data, wordEntry, showAll);
    const shownOccurrences = shownWords.reduce(
      (sum, item) => sum + item.count,
      0,
    );
    document.getElementById("coverage-words-shown").textContent =
      `${number(shownWords.length)} / ${number(wordEntry.words.length / 2)}`;
    document.getElementById("coverage-occurrences-shown").textContent =
      `${number(shownOccurrences)} / ${number(wordEntry.unfamiliar)}`;
    showAllButton.hidden = wordEntry.words.length <= 20;
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
  }

  function warmDetails() {
    if (preloading) return;
    // Follow the visible sort order, keeping two background downloads active.
    const ids = [...grid.querySelectorAll("[data-coverage-id]")]
      .map((button) => button.dataset.coverageId)
      .filter((id) => coverageData.revisions.get(id) && !coverageData.peek(id));
    if (!ids.length) return;
    preloading = coverageData.preload(ids).finally(() => { preloading = null; });
  }

  async function loadLibrary() {
    const request = ++libraryRequest;
    clearTimeout(analysisPoll);
    try {
      const result = await fetchJson(`/api/coverage/${kind}`);
      if (request !== libraryRequest) return;
      window.LocalVocabulary.setWords(result.local_words, result.local_revision);
      showScores(result.items);
      coverageData.setRevisions(result.items);
      warmDetails();
      const selectedId = selectedButton?.dataset.coverageId;
      if (dialog.open && (!data || data.revision !== result.items[selectedId]?.revision)) {
        void loadDetail(true);
      }
      analysisPoll = setTimeout(() => void loadLibrary(), result.refreshing ? 500 : 5000);
    } catch (error) {
      for (const button of buttons) button.title = error.message;
      analysisPoll = setTimeout(() => void loadLibrary(), 5000);
    }
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
      const id = button.dataset.coverageId;
      const score = coverageData.peek(id) || await coverageData.load(id);
      if (currentRequest !== requestNumber) return;
      updateButton(button, score);
      grid.dispatchEvent(new Event("coverage:updated"));
      if (score.status !== "ready") {
        results.hidden = true;
        data = null;
        message.textContent = statuses[score.status] || statuses.error;
        return;
      }
      data = score;
      message.textContent = "";
      results.hidden = false;
      render();
      selectWindow(selected);
    } catch (error) {
      if (currentRequest === requestNumber) message.textContent = error.message;
    }
  }

  for (const button of buttons) {
    const preload = () => void coverageData.load(button.dataset.coverageId).catch(() => {});
    button.addEventListener("pointerenter", preload);
    button.addEventListener("focus", preload);
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      selectedButton = button;
      itemDictionary = null;
      dictionaryRequest++;
      generatedDictionary = null;
      dictionarySearch.value = "";
      dictionaryEntries.replaceChildren();
      data = null;
      selectTab(false);
      document.getElementById("coverage-panel").scrollTop = 0;
      document.getElementById("custom-dictionary-panel").scrollTop = 0;
      showAll = false;
      wholeItem = false;
      document.getElementById("coverage-item-title").textContent =
        button.dataset.coverageTitle;
      dialog.showModal();
      void loadDetail();
      void loadItemDictionary().catch(() => {});
    });
  }
  document
    .getElementById("coverage-close")
    .addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    requestNumber++;
    dictionaryRequest++;
    lookup.close();
  });
  document.getElementById("coverage-lookup-close")
    .addEventListener("click", () => lookup.close());
  lookup.addEventListener("close", () => {
    lookupRequest++;
    lookupRenderer?.reset();
  });
  window.addEventListener("mogao:known-words-changed", () => {
    void loadLibrary();
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
    // Warm dictionary lookups independently of coverage downloads.
    void loadDictionary().catch(() => {});
    coverageData.setRevisions(Object.fromEntries(buttons.map((button) => [
      button.dataset.coverageId,
      { revision: button.closest("[data-sort-card]").dataset.coverageRevision },
    ])));
    warmDetails();
    void loadLibrary();
  }
}

if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", initializeCoverage);
if (typeof module !== "undefined") module.exports = CoverageData;
