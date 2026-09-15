function getLookupRoot(startNode) {
  return startNode.parentElement?.closest("[data-lookup-root]") || bookContent;
}

const LOOKUP_NON_READING_SELECTOR = "rt,rp,script,style";

function isReadingTextNode(node) {
  return !node.parentElement?.closest(LOOKUP_NON_READING_SELECTOR);
}

function createReadingTextWalker(root) {
  return document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      return isReadingTextNode(node)
        ? NodeFilter.FILTER_ACCEPT
        : NodeFilter.FILTER_REJECT;
    },
  });
}

function getTreeWalker(startNode) {
  const walker = createReadingTextWalker(getLookupRoot(startNode));
  walker.currentNode = startNode;
  return walker;
}

function getSmartSnippet(startNode, startOffset) {
  if (!isReadingTextNode(startNode)) return "";

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

function getSentenceSuffix(startNode, startOffset) {
  const originBlock = getBlockParent(startNode);
  const lookupRoot = getLookupRoot(startNode);
  const walker = createReadingTextWalker(lookupRoot);
  walker.currentNode = startNode;

  let node = startNode;
  let offset = startOffset;
  let text = "";

  while (true) {
    if (offset === node.textContent.length) {
      const nextNode = walker.nextNode();
      if (!nextNode || getBlockParent(nextNode) !== originBlock) break;
      node = nextNode;
      offset = 0;
      continue;
    }

    const char = node.textContent[offset];
    if (char === '"' || char === "'") {
      // Straight quotes can also open the next sentence. Only consume a
      // closing quote when a matching quote precedes it in this block.
      const backWalker = createReadingTextWalker(lookupRoot);
      backWalker.currentNode = node;
      let previousNode = node;
      let precedingText = node.textContent.substring(0, offset);
      let quoteCount = 0;
      while (previousNode) {
        quoteCount += precedingText.split(char).length - 1;
        previousNode = backWalker.previousNode();
        if (!previousNode || getBlockParent(previousNode) !== originBlock) break;
        precedingText = previousNode.textContent;
      }
      if (quoteCount % 2 === 0) break;
    } else if (!/[”’»」』)）】\]\s]/.test(char)) {
      break;
    }

    text += char;
    offset++;
  }

  return { text, node, offset };
}

function getSentence(startNode, startOffset) {
	if (!isReadingTextNode(startNode)) return "";

	const terminators = /[。！？.!?]/;
	const originBlock = getBlockParent(startNode);
	const lookupRoot = getLookupRoot(startNode);

	// Walk Backwards (Find Start)
	let leftText = "";
	const backWalker = createReadingTextWalker(lookupRoot);
	backWalker.currentNode = startNode;

	let curr = startNode;
	let partial = curr.textContent.substring(0, startOffset);
	let splitIdx = partial.split("").reverse().join("").search(terminators);

	if (splitIdx !== -1) {
	  const boundaryOffset = partial.length - splitIdx;
	  const suffix = getSentenceSuffix(curr, boundaryOffset);
	  leftText = partial.substring(boundaryOffset + suffix.text.length);
	} else {
	  leftText = partial;
	  while ((curr = backWalker.previousNode())) {
		if (getBlockParent(curr) !== originBlock) break;

		let txt = curr.textContent;
		let sIdx = txt.split("").reverse().join("").search(terminators);
		if (sIdx !== -1) {
		  const boundaryOffset = txt.length - sIdx;
		  const suffix = getSentenceSuffix(curr, boundaryOffset);
		  leftText = (txt.substring(boundaryOffset) + leftText).substring(
		    suffix.text.length,
		  );
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

		// Keep closing quotes/spaces on the same side of both boundaries.
		const boundaryOffset = curr.textContent.length - textToProcess.length + endIdx;
		const suffix = getSentenceSuffix(curr, boundaryOffset);
		const peekNode = suffix.node;
		const peekTxt = peekNode.textContent.substring(suffix.offset);
		const extraStr = suffix.text;

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
