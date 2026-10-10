document.addEventListener("DOMContentLoaded", () => {
  const vocabulary = window.LocalVocabulary;
  const list = document.getElementById("known-words-list");
  if (!list || !vocabulary) return;
  const search = document.getElementById("known-words-search");
  const more = document.getElementById("known-words-more");
  const notice = document.getElementById("known-words-notice");
  const dialog = document.getElementById("coverage-lookup");
  const body = document.getElementById("coverage-lookup-body");
  const renderer = createDictionaryPopupRenderer(body);
  const findWords = createVocabularySearch();
  let limit = 100;
  let lookupRequest = 0;
  let searchLoading = false;
  let searchError = false;

  function render() {
    const words = vocabulary.list();
    const query = search.value.trim();
    if (window.localDict) searchError = false;
    const matching = findWords(words, query, window.localDict);
    list.replaceChildren();
    for (const word of matching.slice(0, limit)) {
      const row = document.createElement("li");
      const lookup = document.createElement("button");
      lookup.type = "button";
      lookup.className = "known-word-lookup";
      lookup.textContent = word;
      lookup.title = `Look up ${word}`;
      lookup.addEventListener("click", () => void openLookup(word));
      const unmark = document.createElement("button");
      unmark.type = "button";
      unmark.className = "local-known-toggle";
      unmark.textContent = "Forget";
      unmark.setAttribute("aria-label", `Forget ${word}`);
      unmark.addEventListener("click", () => void vocabulary.setKnown(word, false, unmark, notice));
      row.append(lookup, unmark);
      list.appendChild(row);
    }
    document.getElementById("known-words-count").textContent =
      query ? `${matching.length} / ${words.length} words` : `${words.length} ${words.length === 1 ? "word" : "words"}`;
    document.getElementById("known-words-search-status").textContent = !query ? "" :
      searchLoading ? "Loading dictionary…" : searchError ? "Dictionary unavailable. Type to retry." : "";
    list.setAttribute("aria-busy", String(!!query && searchLoading));
    const empty = document.getElementById("known-words-empty");
    empty.hidden = matching.length > 0 || (!!query && searchLoading);
    empty.textContent = query ? "No matching words." : "No words marked yet.";
    more.hidden = matching.length <= limit;
  }

  async function openLookup(word) {
    const request = ++lookupRequest;
    if (!dialog.open) dialog.showModal();
    body.replaceChildren();
    const message = document.createElement("div");
    message.className = "dictionary-message";
    message.textContent = "Loading…";
    body.appendChild(message);
    try {
      await loadDictionary();
      if (request !== lookupRequest) return;
      renderer.show([dictionaryResultForWord(word) || {
        word, entries: [{ pinyin: "", definitions: ["No definition found."] }],
      }]);
    } catch {
      if (request === lookupRequest) message.textContent = "Could not load dictionary. Close and try again.";
    }
  }

  search.addEventListener("input", async () => {
    limit = 100;
    if (!search.value.trim() || window.localDict || searchLoading) { render(); return; }
    searchLoading = true;
    searchError = false;
    render();
    try {
      await loadDictionary();
    } catch {
      searchError = true;
    } finally {
      searchLoading = false;
      render(); // Use the current query, even if it changed during loading.
    }
  });
  more.addEventListener("click", () => { limit += 100; render(); });
  document.getElementById("coverage-lookup-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => { lookupRequest++; renderer.reset(); });
  window.addEventListener("mogao:known-words-changed", render);
  // Refresh after returning from another page/device; cross-tab changes arrive live.
  window.addEventListener("focus", async () => {
    try {
      const response = await fetch("/api/known-words", { cache: "no-store" });
      if (response.status === 404) { window.location.reload(); return; }
      if (!response.ok) return;
      const state = await response.json();
      vocabulary.setWords(state.words, state.revision, true);
    } catch {} // Keep the displayed list when temporarily offline.
  });
  render();
});
