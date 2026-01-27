
  const popup = document.getElementById("def-popup");
  const popBody = document.getElementById("pop-body");
  const contentContainer = document.querySelector(".content-container");
  const bookContent = document.querySelector(".book-content");
  const deckWords = new Set(window.MOGAO_CONFIG.deckWords); 

  let currentHighlightSpans = [];
  let currentSentence = "";


  function closePopup() {
	resetUI({ keepSpacer: false });
  }

  function resetUI({ keepSpacer } = { keepSpacer: false }) {
	if (!keepSpacer) {
	  popup.classList.remove("visible");
	  contentContainer.classList.remove("has-popup");
	}
	currentHighlightSpans.forEach((span) => {
	  const parent = span.parentNode;
	  if (parent) {
		while (span.firstChild) parent.insertBefore(span.firstChild, span);
		parent.removeChild(span);
	  }
	});
	currentHighlightSpans = [];
	if (!keepSpacer) bookContent.normalize();
  }


  let isAutoScrolling = false;
  function adjustScroll() {
	if (currentHighlightSpans.length === 0) return;
	const lastSpan =
	  currentHighlightSpans[currentHighlightSpans.length - 1];
	const rect = lastSpan.getBoundingClientRect();
	const viewHeight = window.innerHeight;
	const popupTop = viewHeight * 0.45;
	const safeLimit = popupTop - 40;
	if (rect.bottom > safeLimit) {
	  const diff = rect.bottom - safeLimit;

	  isAutoScrolling = true;
	  const mainEl = document.getElementById("main");
	  mainEl.style.scrollBehavior = "auto";
	  mainEl.scrollBy({ top: diff, behavior: "auto" });
	}
	setTimeout(() => {
	  const mainEl = document.getElementById("main");
	  isAutoScrolling = false;
	  mainEl.style.scrollBehavior = "smooth";
	}, 50);
  }


  // Main click handler
  bookContent.addEventListener("click", async function (e) {
	if (!localDict) {
	  return;
	}
	let range,
	  x = e.clientX,
	  y = e.clientY;
	if (document.caretRangeFromPoint)
	  range = document.caretRangeFromPoint(x, y);
	else if (document.caretPositionFromPoint) {
	  const pos = document.caretPositionFromPoint(x, y);
	  range = document.createRange();
	  range.setStart(pos.offsetNode, pos.offset);
	  range.collapse(true);
	}

	let hasChinese = false;
	let textChunk = "";
	let startNode, startOffset;

	if (range && range.startContainer.nodeType === Node.TEXT_NODE) {
	  startNode = range.startContainer;
	  startOffset = range.startOffset;

	  if (startOffset > 0) {
		const tempRange = document.createRange();
		// select the character immediately before the cursor
		tempRange.setStart(startNode, startOffset - 1);
		tempRange.setEnd(startNode, startOffset);

		// get the bounding box of that character
		const rects = tempRange.getClientRects();
		for (const rect of rects) {
		  // Check if the click (x, y) is actually inside that character's box
		  if (
			x >= rect.left &&
			x <= rect.right &&
			y >= rect.top &&
			y <= rect.bottom
		  ) {
			startOffset -= 1; // It was a right-side click, so move back 1
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
	contentContainer.classList.add("has-popup");
	popBody.innerHTML =
	  '<div style="padding:30px; text-align:center; color:#999;">Searching...</div>';
	popup.classList.add("visible");

	const results = performLookup(textChunk);

	popBody.innerHTML = "";
	if (results && results.length > 0) {
	  const longest = results[0];
	  highlightRange(startNode, startOffset, longest.length);
	  adjustScroll();
	  results.forEach((item) => {
		const div = document.createElement("div");
		div.className = "result-item";

		let html = "";
		item.entries.forEach((entry) => {

		let freqHtml = item.frequency
		  ? `<span class="freq-badge">#${item.frequency}</span>`
		  : "";
		let definitionsHTML = `<ul>`;
		definitionsHTML += entry.definitions.map((d) => `<li>${d}</li>`).join("");
		definitionsHTML += `</ul>`;
		
		const encodedDefinitions = btoa(unescape(encodeURIComponent(definitionsHTML)));
		let ankiBtn = `<button 
			class="anki-btn" 
			${deckWords.has(item.word) ? 'disabled' : ''} 
			data-word="${item.word}" 
			data-pinyin="${entry.pinyin}"
			data-defs="${encodedDefinitions}"
			onclick="addToAnki(this)">
			${deckWords.has(item.word) ? '✓' : '+'}
			</button>`;
		html += `<div class="result-head"><span class="word-main">${item.word}</span>${freqHtml}${ankiBtn}</div>`;
		  let defsHtml = entry.definitions
			.map((d) => `<li>${d}</li>`)
			.join("");
			html += `<div class="entry-block"><div class="entry-pinyin">${entry.pinyin}</div><ul class="entry-defs">${defsHtml}</ul></div>`;
		});
		div.innerHTML = html;
		popBody.appendChild(div);
	  });
	} else {
	  popBody.innerHTML =
		'<div style="padding:30px; text-align:center; color:#999;">No definition found.</div>';
	}
  });


  // Close popup on scroll
  mainContent.addEventListener("scroll", () => {
	if (isAutoScrolling) return;
	if (popup.classList.contains("visible")) closePopup();
  });

