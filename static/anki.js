const suspendedAnkiWords = new Set(window.MOGAO_CONFIG.suspendedWords ?? []);

window.configureAnkiButton = function (button, word) {
  const suspended = suspendedAnkiWords.has(word);
  button.disabled = deckWords.has(word) && !suspended;
  button.classList.toggle("anki-btn-suspended", suspended);
  button.dataset.replaceSuspended = String(suspended);
  if (suspended) {
    button.innerHTML = `<svg width="17" height="17" viewBox="0 0 24 24"
      fill="none" stroke="currentColor" stroke-width="2.8"
      stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M20.1 9a8.5 8.5 0 1 0 .4 5" />
      <path d="M21 3v6h-6" />
    </svg>`;
  } else {
    button.textContent = button.disabled ? "✓" : "+";
  }
  const label = suspended
    ? `Replace suspended card for ${word} with this sentence`
    : `Add ${word} to Anki`;
  button.setAttribute("aria-label", label);
  button.title = label;
};

window.addToAnki = function (btn, id) {
  const word = btn.getAttribute("data-word");
  const replaceSuspended = btn.dataset.replaceSuspended === "true";
  const pinyin = btn.getAttribute("data-pinyin");
  const definitionsHTML = decodeURIComponent(
    escape(atob(btn.getAttribute("data-defs"))),
  );

  // Update ALL buttons in the popup for this specific word
  const allMatchingButtons = [...document.querySelectorAll(".anki-btn")].filter(
    (button) => button.dataset.word === word,
  );
  allMatchingButtons.forEach((b) => {
    b.classList.remove("anki-btn-suspended");
    b.dataset.replaceSuspended = "false";
    b.textContent = "✓";
    b.disabled = true;
  });
  suspendedAnkiWords.delete(word);
  deckWords.add(word);
  window.markWordKnown?.(word);
  fetch("/api/new-card", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Mogao-Request": "1" },
    body: JSON.stringify({
      book_id: id,
      word: word,
      replace_suspended: replaceSuspended,
      pinyin: pinyin,
      sentence: currentSentence,
      definitions: definitionsHTML,
    }),
  }).catch((err) => console.error("Failed to send to Anki:", err));
};

// TODO: Refactor
window.addToAnkiFromVideo = function (btn, id) {
  const subs = JSON.parse(btn.getAttribute("data-segmentedsubs"));
  const word = btn.getAttribute("data-word");
  const replaceSuspended = btn.dataset.replaceSuspended === "true";
  const pinyin = btn.getAttribute("data-pinyin");
  const definitionsHTML = decodeURIComponent(
    escape(atob(btn.getAttribute("data-defs"))),
  );

  // Update ALL buttons in the popup for this specific word
  const allMatchingButtons = [...document.querySelectorAll(".anki-btn")].filter(
    (button) => button.dataset.word === word,
  );
  allMatchingButtons.forEach((b) => {
    b.classList.remove("anki-btn-suspended");
    b.dataset.replaceSuspended = "false";
    b.textContent = "✓";
    b.disabled = true;
  });
  suspendedAnkiWords.delete(word);
  deckWords.add(word);
  window.markWordKnown?.(word);
  fetch(`${window.MOGAO_CONFIG.videoBasePath}/api/new-card-from-video`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Mogao-Request": "1" },
    body: JSON.stringify({
      book_id: id,
      word: word,
      replace_suspended: replaceSuspended,
      pinyin: pinyin,
      sentence: subs.text,
      definitions: definitionsHTML,
      start: subs.start,
      end: subs.end,
    }),
  }).catch((err) => console.error("Failed to send to Anki:", err));
};
