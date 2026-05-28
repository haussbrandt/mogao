window.addToAnki = function (btn, id) {
  const word = btn.getAttribute("data-word");
  const pinyin = btn.getAttribute("data-pinyin");
  const definitionsHTML = decodeURIComponent(
    escape(atob(btn.getAttribute("data-defs"))),
  );

  // Update ALL buttons in the popup for this specific word
  const allMatchingButtons = document.querySelectorAll(
    `.anki-btn[data-word="${word}"]`,
  );
  allMatchingButtons.forEach((b) => {
    b.textContent = "✓";
    b.disabled = true;
  });
  deckWords.add(word);
  fetch("/api/new-card", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      book_id: id,
      word: word,
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
  const pinyin = btn.getAttribute("data-pinyin");
  const definitionsHTML = decodeURIComponent(
    escape(atob(btn.getAttribute("data-defs"))),
  );

  // Update ALL buttons in the popup for this specific word
  const allMatchingButtons = document.querySelectorAll(
    `.anki-btn[data-word="${word}"]`,
  );
  allMatchingButtons.forEach((b) => {
    b.textContent = "✓";
    b.disabled = true;
  });
  deckWords.add(word);
  fetch("/api/new-card-from-video", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      book_id: id,
      word: word,
      pinyin: pinyin,
      sentence: currentSentence,
      definitions: definitionsHTML,
      start: subs.start,
      end: subs.end,
    }),
  }).catch((err) => console.error("Failed to send to Anki:", err));
};
