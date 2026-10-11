(() => {
  const enabled = window.MOGAO_CONFIG?.ankiEnabled ?? (document.body.dataset.ankiEnabled === "true");
  if (!enabled) return;
  let words = new Set(Object.keys(window.MOGAO_CONFIG?.localKnownWords || {}));
  let revision = window.MOGAO_CONFIG?.localKnownRevision ?? -1;
  const channel =
    typeof BroadcastChannel === "function"
      ? new BroadcastChannel("mogao-known-words")
      : null;

  function setWords(values, nextRevision, notify = false) {
    if (!Number.isSafeInteger(nextRevision) || nextRevision < revision) return false;
    if (nextRevision === revision) return true;
    revision = nextRevision;
    words = new Set(Object.keys(values));
    if (window.MOGAO_CONFIG) {
      window.MOGAO_CONFIG.localKnownWords = values;
      window.MOGAO_CONFIG.localKnownRevision = revision;
    }
    document.querySelectorAll("[data-local-known-word]").forEach(updateButton);
    if (notify) {
      try {
        sessionStorage.removeItem("mogao:coverage:books");
        sessionStorage.removeItem("mogao:coverage:videos");
      } catch {}
      window.dispatchEvent(new CustomEvent("mogao:known-words-changed"));
    }
    return true;
  }

  function updateButton(button) {
    const known = words.has(button.dataset.localKnownWord);
    button.hidden =
      !known &&
      !!window.MOGAO_CONFIG?.knownWords?.includes(
        button.dataset.localKnownWord,
      );
    button.textContent = known ? "Known" : "Unknown";
    button.title = known ? "Unmark as known" : "Mark as known";
    button.setAttribute("aria-label", `${button.title}: ${button.dataset.localKnownWord}`);
    button.setAttribute("aria-pressed", String(known));
  }

  function notice(parent, text) {
    document
      .querySelectorAll(".local-known-notice")
      .forEach((node) => node.remove());
    const box = document.createElement("div");
    box.className = "local-known-notice";
    box.setAttribute("role", "status");
    box.append(document.createTextNode(text + " "));
    const close = document.createElement("button");
    close.type = "button";
    close.className = "local-known-action";
    close.textContent = "×";
    close.setAttribute("aria-label", "Dismiss");
    close.addEventListener("click", () => box.remove());
    box.appendChild(close);
    parent.appendChild(box);
  }

  async function changeWord(word, known, button, parent) {
    button.disabled = true;
    document.querySelectorAll(".local-known-notice").forEach((node) => node.remove());
    try {
      const raw = known ? window.localDict?.[word] : null;
      const definition = raw?.e?.length ? {
        e: raw.e.map((entry) => ({ p: entry.p || "", d: entry.d || [] })),
        llm: !!raw.llm,
      } : undefined;
      const response = await fetch("/api/known-words", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Mogao-Request": "1" },
        body: JSON.stringify({ word, known, definition }),
      });
      if (!response.ok) throw new Error("Could not save this word. Try again.");
      const result = await response.json();
      if (!setWords(result.words, result.revision, true)) return;
      channel?.postMessage(result);
    } catch (error) {
      notice(parent, error.message);
    } finally {
      button.disabled = false;
    }
  }

  function createButton(word) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "local-known-toggle";
    button.dataset.localKnownWord = word;
    updateButton(button);
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const parent = button.closest("dialog, #def-popup") || document.body;
      void changeWord(word, !words.has(word), button, parent);
    });
    return button;
  }

  if (channel)
    channel.onmessage = (event) =>
      setWords(event.data.words, event.data.revision, true);
  window.LocalVocabulary = {
    createButton,
    setWords,
    has: (word) => words.has(word),
    list: () => [...words],
    setKnown: changeWord,
  };
})();
