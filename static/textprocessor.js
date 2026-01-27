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
