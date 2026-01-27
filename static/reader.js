




  const popup = document.getElementById("def-popup");
  const popBody = document.getElementById("pop-body");
  const contentContainer = document.querySelector(".content-container");
  const bookContent = document.querySelector(".book-content");
  const deckWords = new Set(window.MOGAO_CONFIG.deckWords); 

  let currentHighlightSpans = [];
  let currentSentence = "";

  let localDict = null;

  fetch("/static/dict.json?v=2")
	.then((r) => r.json())
	.then((data) => {
	  localDict = data;
	  console.log("Dictionary loaded");
	})
	.catch((err) => console.error("Failed to load dictionary", err));

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

  function getTreeWalker(startNode) {
	const walker = document.createTreeWalker(
	  bookContent,
	  NodeFilter.SHOW_TEXT,
	  null,
	  false,
	);
	walker.currentNode = startNode;
	return walker;
  }

  function getSmartSnippet(startNode, startOffset) {
	let text = "";
	const walker = getTreeWalker(startNode);
	let node = startNode;
	let currentOffset = startOffset;
	while (node && text.length < 8) {
	  let val = node.textContent;
	  text += val.substring(currentOffset);
	  node = walker.nextNode();
	  currentOffset = 0;
	}
	return text;
  }

  function getBlockParent(node) {
	if (!node || !node.parentElement) return null;
	return node.parentElement.closest(
	  "p, div, h1, h2, h3, h4, h5, h6, li, blockquote",
	);
  }

  function getSentence(startNode, startOffset) {
	const terminators = /[。！？.!?]/;
	const closingQuotes = /[""''»」)）】\]]/;
	const originBlock = getBlockParent(startNode);

	// Walk Backwards (Find Start)
	let leftText = "";
	const backWalker = document.createTreeWalker(
	  bookContent,
	  NodeFilter.SHOW_TEXT,
	  null,
	  false,
	);
	backWalker.currentNode = startNode;

	let curr = startNode;
	let partial = curr.textContent.substring(0, startOffset);
	let splitIdx = partial.split("").reverse().join("").search(terminators);

	if (splitIdx !== -1) {
	  leftText = partial.substring(partial.length - splitIdx);
	} else {
	  leftText = partial;
	  while ((curr = backWalker.previousNode())) {
		if (getBlockParent(curr) !== originBlock) break;

		let txt = curr.textContent;
		let sIdx = txt.split("").reverse().join("").search(terminators);
		if (sIdx !== -1) {
		  leftText = txt.substring(txt.length - sIdx) + leftText;
		  break;
		} else {
		  leftText = txt + leftText;
		}
		if (leftText.length > 200) break;
	  }
	}

	// Walk Forwards (Find End)
	let rightText = "";
	const fwdWalker = getTreeWalker(startNode);
	curr = startNode;
	let textToProcess = curr.textContent.substring(startOffset);

	while (true) {
	  if (!textToProcess) {
		curr = fwdWalker.nextNode();
		if (!curr || getBlockParent(curr) !== originBlock) break;
		textToProcess = curr.textContent;
		continue;
	  }

	  let match = textToProcess.match(terminators);

	  if (match) {
		let termIdx = match.index;
		let endIdx = termIdx + 1;

		let chunk = textToProcess.substring(0, endIdx);

		// Look ahead for closing quotes/spaces
		let tempWalker = document.createTreeWalker(
		  bookContent,
		  NodeFilter.SHOW_TEXT,
		  null,
		  false,
		);
		tempWalker.currentNode = curr;
		let peekNode = curr;
		let peekTxt = textToProcess.substring(endIdx);
		let extraStr = "";

		while (true) {
		  if (!peekTxt) {
			peekNode = tempWalker.nextNode();
			if (!peekNode || getBlockParent(peekNode) !== originBlock)
			  break;
			peekTxt = peekNode.textContent;
			continue;
		  }
		  let char = peekTxt[0];
		  if (char.match(closingQuotes) || char.match(/\s/)) {
			extraStr += char;
			peekTxt = peekTxt.substring(1);
		  } else {
			break;
		  }
		}

		let candidateSentence = (
		  leftText +
		  rightText +
		  chunk +
		  extraStr
		).trim();

		// Heuristic: If sentence is < 5 chars, merge with next
		if (candidateSentence.length < 5) {
		  rightText += chunk + extraStr;
		  curr = peekNode;
		  textToProcess = peekTxt;
		  fwdWalker.currentNode = curr;
		  continue;
		} else {
		  rightText += chunk + extraStr;
		  break;
		}
	  } else {
		rightText += textToProcess;
		textToProcess = "";
		if (rightText.length > 500) break;
	  }
	}

	return (leftText + rightText)
	  .replace(/[\r\n\t]+/g, "")
	  .replace(/\s+/g, " ")
	  .trim();
  }

  function highlightRange(startNode, startOffset, length) {
	const nodesToWrap = [];
	let charsNeeded = length;
	const walker = getTreeWalker(startNode);
	let currentNode = startNode;
	let currentOffset = startOffset;
	while (charsNeeded > 0 && currentNode) {
	  const available = currentNode.textContent.length - currentOffset;
	  const take = Math.min(charsNeeded, available);
	  if (take > 0) {
		nodesToWrap.push({
		  node: currentNode,
		  start: currentOffset,
		  end: currentOffset + take,
		});
		charsNeeded -= take;
	  }
	  if (charsNeeded > 0) {
		currentNode = walker.nextNode();
		currentOffset = 0;
	  }
	}
	nodesToWrap.forEach((item) => {
	  const range = document.createRange();
	  range.setStart(item.node, item.start);
	  range.setEnd(item.node, item.end);
	  const span = document.createElement("span");
	  span.className = "lookup-highlight";
	  try {
		range.surroundContents(span);
		currentHighlightSpans.push(span);
	  } catch (e) {
		console.log("Highlight error", e);
	  }
	});
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

  function performLookup(text) {
	if (!localDict) return [];

	const cleanText = text.replace(/\s+/g, "");
	const limit = Math.min(6, cleanText.length);
	const results = [];

	for (let i = limit; i > 0; i--) {
	  const candidate = cleanText.substring(0, i);
	  const entry = localDict[candidate];

	  if (entry) {
		// Expand the minified JSON back to usable structure
		let freqStr = null;
		if (entry.f) {
		  if (entry.f < 10000) freqStr = "" + entry.f;
		  else freqStr = entry.f.toLocaleString("en-US").replace(/,/g, " ");
		}

		results.push({
		  word: candidate,
		  entries: entry.e.map((e) => ({ pinyin: e.p, definitions: e.d })),
		  frequency: freqStr,
		  length: i,
		});
	  }
	}
	return results;
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

  window.addToAnki = function (btn) {
	const word = btn.getAttribute('data-word');
	const pinyin = btn.getAttribute('data-pinyin');
	const definitionsHTML = decodeURIComponent(escape(atob(btn.getAttribute('data-defs'))));

	// Update ALL buttons in the popup for this specific word
	const allMatchingButtons = document.querySelectorAll(`.anki-btn[data-word="${word}"]`);
	allMatchingButtons.forEach(b => {
		b.textContent = "✓";
		b.disabled = true;
		});
	deckWords.add(word);
	 fetch("/api/new-card", {
		 method: "POST",
		 headers: { "Content-Type": "application/json" },
		 body: JSON.stringify({
			 word: word,
			 pinyin: pinyin,
			 sentence: currentSentence,
			 definitions: definitionsHTML
			})
	   }).catch(err => console.error("Failed to send to Anki:", err));
  };

  // Close popup on scroll
  mainContent.addEventListener("scroll", () => {
	if (isAutoScrolling) return;
	if (popup.classList.contains("visible")) closePopup();
  });

