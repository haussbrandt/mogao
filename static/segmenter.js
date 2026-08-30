const MAX_WORD_LEN = 8;
const OOV_PENALTY = Math.log(50_001);
const LENGTH_BONUS = 4;
const NO_FREQ_KNOWN = 200;
const NO_FREQ_UNKNOWN = 8000;
const NUMBERS = new Set("零一二三四五六七八九十百千万亿两");
let _known = null;

const PUNCT_RE =
  /^[\s\p{P}\p{Z}\p{C}！？。，、；：""''「」『』【】〔〕…—～·×÷]+$/u;
const NUMBER_RE = /^[0-9０-９]+$/u;

function isPunct(s) {
  return s.length > 0 && (PUNCT_RE.test(s) || NUMBER_RE.test(s));
}

function isCompositionallyKnown(word, known) {
  // Pure Chinese numbers: 一百, 三十四, 两千, 零...
  if (word.length > 0 && [...word].every((ch) => NUMBERS.has(ch))) {
    return true;
  }

  // 们 pluralization: 他→他们, 我→我们, etc.
  if (word.length >= 2 && word.endsWith("们")) {
    return known.has(word.slice(0, -1));
  }

  // 第X ordinals: 第一, 第二, 第三...
  if (word.length >= 2 && word.startsWith("第")) {
    return [...word.slice(1)].every((ch) => NUMBERS.has(ch));
  }

  // 不 + known word negation (skip known idioms)
  const BU_IDIOMS = new Set(["不得了", "不要紧", "不得不", "不由得"]);
  if (word.length >= 2 && word.startsWith("不") && !BU_IDIOMS.has(word)) {
    return known.has(word.slice(1));
  }

  return false;
}

function buildKnownSet() {
  return new Set(window.MOGAO_CONFIG.knownWords);
}

// DP Segmentation
function segment(sentence, dict, known) {
  const n = sentence.length;
  if (n === 0) return [];

  const dp = new Array(n + 1).fill(null);
  dp[0] = { oov: 0, logRank: 0, segs: 0, words: [] };

  for (let i = 1; i <= n; i++) {
    for (let j = Math.max(0, i - MAX_WORD_LEN); j < i; j++) {
      if (!dp[j]) continue;
      const word = sentence.slice(j, i);

      let oovAdd, rankAdd;
      if (isPunct(word)) {
        oovAdd = 0;
        rankAdd = 0;
      } else if (dict[word]) {
        oovAdd = 0;
        const fallback = known.has(word) ? NO_FREQ_KNOWN : NO_FREQ_UNKNOWN;
        const rank = dict[word].f ?? fallback;
        rankAdd = Math.log(rank + 1) - LENGTH_BONUS * (word.length - 1);
      } else {
        oovAdd = word.length;
        rankAdd = OOV_PENALTY * word.length;
      }

      const c = {
        oov: dp[j].oov + oovAdd,
        logRank: dp[j].logRank + rankAdd,
        segs: dp[j].segs + 1,
        words: [...dp[j].words, word],
      };

      if (
        !dp[i] ||
        c.oov < dp[i].oov ||
        (c.oov === dp[i].oov && c.logRank < dp[i].logRank) ||
        (c.oov === dp[i].oov &&
          c.logRank === dp[i].logRank &&
          c.segs < dp[i].segs)
      ) {
        dp[i] = c;
      }
    }
  }

  return dp[n]?.words ?? [...sentence];
}

// Classification

const SENTENCE_END_RE = /(?<=[。！？…]+)/u;
const TEXT_BLOCK_SELECTOR = "p,div,h1,h2,h3,h4,h5,h6,li,blockquote,pre,td,th";
const NON_READING_SELECTOR = "rt,rp,script,style";
const CHINESE_RE = /[\u4e00-\u9fff]/;

function classifyWord(word, dict, known) {
  if (isPunct(word)) return "punct";
  if (!dict[word]) return "oov";
  if (known.has(word) || isCompositionallyKnown(word, known)) return "known";
  return "unknown";
}

/**
 * Analyze a single sentence. If there is exactly one unknown word and no OOV
 * segments, promote that word to "i1" — a true i+1 target.
 */
function analyzeSentence(sentence, dict, known) {
  const words = segment(sentence, dict, known).map((w) => ({
    word: w,
    status: classifyWord(w, dict, known),
  }));

  const unknowns = words.filter((w) => w.status === "unknown");
  const hasOov = words.some((w) => w.status === "oov");

  if (unknowns.length === 1 && !hasOov) {
    unknowns[0].status = "i1";
  }

  return words;
}

function splitSentences(text) {
  return text.split(SENTENCE_END_RE).filter((s) => s.length > 0);
}

const STATUS_CLASS = {
  known: "seg-known",
  unknown: "seg-unknown",
  i1: "seg-i1",
  oov: "seg-oov",
};

function getTextBlock(node, root) {
  let element = node.parentElement;
  while (element && element !== root) {
    if (element.matches(TEXT_BLOCK_SELECTOR)) return element;
    element = element.parentElement;
  }
  return root;
}

function analyzeText(text, dict, known, nextTokenId) {
  const tokens = [];
  let sentenceStart = 0;

  for (const sentence of splitSentences(text)) {
    let tokenStart = sentenceStart;
    for (const { word, status } of analyzeSentence(sentence, dict, known)) {
      tokens.push({
        start: tokenStart,
        end: tokenStart + word.length,
        status,
        id: String(nextTokenId.value++),
      });
      tokenStart += word.length;
    }
    sentenceStart += sentence.length;
  }

  return tokens;
}

function annotateTextNodes(nodes, dict, known, nextTokenId) {
  const text = nodes.map((node) => node.textContent).join("");
  if (!CHINESE_RE.test(text)) return;

  const tokens = analyzeText(text, dict, known, nextTokenId);
  let nodeStart = 0;
  let tokenIndex = 0;

  for (const textNode of nodes) {
    const originalText = textNode.textContent;
    const nodeEnd = nodeStart + originalText.length;
    const frag = document.createDocumentFragment();
    let localOffset = 0;

    while (tokenIndex < tokens.length && tokens[tokenIndex].end <= nodeStart) {
      tokenIndex++;
    }

    let currentTokenIndex = tokenIndex;
    while (
      currentTokenIndex < tokens.length &&
      tokens[currentTokenIndex].start < nodeEnd
    ) {
      const token = tokens[currentTokenIndex];
      const overlapStart = Math.max(token.start, nodeStart) - nodeStart;
      const overlapEnd = Math.min(token.end, nodeEnd) - nodeStart;

      if (overlapStart > localOffset) {
        frag.appendChild(
          document.createTextNode(
            originalText.slice(localOffset, overlapStart),
          ),
        );
      }

      const fragmentText = originalText.slice(overlapStart, overlapEnd);
      if (token.status === "punct" || !CHINESE_RE.test(fragmentText)) {
        frag.appendChild(document.createTextNode(fragmentText));
      } else {
        const span = document.createElement("span");
        span.className = STATUS_CLASS[token.status];
        span.dataset.segToken = token.id;
        span.textContent = fragmentText;
        frag.appendChild(span);
      }

      localOffset = overlapEnd;
      currentTokenIndex++;
    }

    if (localOffset < originalText.length) {
      frag.appendChild(
        document.createTextNode(originalText.slice(localOffset)),
      );
    }

    textNode.parentNode.replaceChild(frag, textNode);
    nodeStart = nodeEnd;
  }
}

function annotateNode(root, dict, known) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (
        node.parentElement?.closest(".seg-known,.seg-unknown,.seg-i1,.seg-oov")
      ) {
        return NodeFilter.FILTER_REJECT;
      }
      if (node.parentElement?.closest(NON_READING_SELECTOR)) {
        return NodeFilter.FILTER_REJECT;
      }
      return node.textContent.length > 0
        ? NodeFilter.FILTER_ACCEPT
        : NodeFilter.FILTER_SKIP;
    },
  });

  const nodeGroups = [];
  let currentGroup = null;
  while (walker.nextNode()) {
    const node = walker.currentNode;
    const block = getTextBlock(node, root);
    if (!currentGroup || currentGroup.block !== block) {
      currentGroup = { block, nodes: [] };
      nodeGroups.push(currentGroup);
    }
    currentGroup.nodes.push(node);
  }

  const nextTokenId = { value: 0 };
  for (const { nodes } of nodeGroups) {
    annotateTextNodes(nodes, dict, known, nextTokenId);
  }
}

function stripAnnotations(root) {
  for (const span of root.querySelectorAll(
    ".seg-known,.seg-unknown,.seg-i1,.seg-oov",
  )) {
    const p = span.parentNode;
    while (span.firstChild) p.insertBefore(span.firstChild, span);
    p.removeChild(span);
  }
  root.normalize();
}

function buildUI(bookContent) {
  const toggleBtn = document.getElementById("seg-toggle");

  if (toggleBtn && !toggleBtn.dataset.segInitialized) {
    const MODES = ["i1", "all", "off"];

    toggleBtn.addEventListener("click", () => {
      let currentMode = bookContent.getAttribute("data-seg-mode") || "i1";
      let nextMode = MODES[(MODES.indexOf(currentMode) + 1) % MODES.length];

      bookContent.setAttribute("data-seg-mode", nextMode);
      refreshStatsPopup(bookContent);
    });
    toggleBtn.dataset.segInitialized = "true";
  }
}

// Public entry point — call from dictionary.js after localDict is set
async function annotateBookContent(dict, known) {
  const bookContent = document.querySelector(".book-content");
  if (!bookContent) return;

  _known = known;

  // Initialize the mode to 'i1' by default if not set
  if (!bookContent.hasAttribute("data-seg-mode")) {
    bookContent.setAttribute("data-seg-mode", "i1");
  }

  // Parse text only once; TreeWalker ignores elements already wrapped in '.seg-'
  annotateNode(bookContent, dict, known);
  buildUI(bookContent);
}

window.reannotateWithNewDict = function (newDict) {
  if (!newDict || !_known) return;
  const bookContent = document.querySelector(".book-content");
  if (!bookContent) return;

  stripAnnotations(bookContent);
  annotateNode(bookContent, newDict, _known);
  if (typeof refreshStatsPopup === "function") {
    refreshStatsPopup(bookContent);
  }
};

window.markWordKnown = function (word) {
  if (!word || !_known) return;

  _known.add(word);
  window.reannotateWithNewDict(window.localDict);
};
