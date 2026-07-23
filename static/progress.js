let totalChars = 0;
let ignoreNextScroll = false;
const charProgressDisplay = document.getElementById("char-progress");
const bookContentElement = document.getElementsByClassName("book-content")[0];

function countChineseChars(str) {
  const matches = str.match(/[\u4e00-\u9fff]/g);
  return matches ? matches.length : 0;
}

// TODO: It's now precalculated when adding a book, so this can be removed
function countTotalCharacters() {
  if (!bookContentElement) return 0;
  const text =
    bookContentElement.textContent || bookContentElement.innerText || "";
  totalChars = countChineseChars(text);
  return totalChars;
}

function countReadCharacters() {
  if (!bookContentElement) return 0;

  const mainEl = document.getElementById("main");
  if (!mainEl) return 0;

  const containerRect = mainEl.getBoundingClientRect();
  const scrollTop = mainEl.scrollTop;
  const viewportBottom = scrollTop + containerRect.height;

  let charCount = 0;
  const walker = document.createTreeWalker(
    bookContentElement,
    NodeFilter.SHOW_TEXT,
    null,
    false,
  );

  let node;
  while ((node = walker.nextNode())) {
    const text = node.textContent;
    if (!text || text.trim().length === 0) continue;

    const parent = node.parentElement;
    if (!parent) continue;

    // Get the bounding rect of the parent element
    const parentRect = parent.getBoundingClientRect();
    const parentTop = parentRect.top - containerRect.top + scrollTop;
    const parentBottom = parentRect.bottom - containerRect.top + scrollTop;

    // If the entire element is above the viewport bottom, count all its characters
    if (parentBottom <= viewportBottom) {
      charCount += countChineseChars(text);
    }
    // If the element is partially visible, we need to estimate or measure more precisely
    else if (parentTop < viewportBottom) {
      // Element spans the viewport bottom - need more precise measurement
      const range = document.createRange();
      range.selectNodeContents(node);

      const textByteLength = text.length;

      // Binary search to find the last visible character
      let left = 0;
      let right = textByteLength;
      let lastVisible = 0;

      while (left < right) {
        const mid = Math.floor((left + right) / 2);
        range.setStart(node, 0);
        range.setEnd(node, mid);

        const rects = range.getClientRects();
        if (rects.length === 0) {
          left = mid + 1;
          continue;
        }

        const lastRect = rects[rects.length - 1];
        const rectBottom = lastRect.bottom - containerRect.top + scrollTop;

        if (rectBottom <= viewportBottom) {
          lastVisible = mid;
          left = mid + 1;
        } else {
          right = mid;
        }
      }

      // Count characters up to lastVisible position
      const visibleText = text.substring(0, lastVisible);
      charCount += countChineseChars(visibleText);

      // Once we hit a partially visible element, everything after is not visible
      break;
    } else {
      // Element is completely below viewport - stop counting
      break;
    }
  }

  return charCount;
}

function formatNumber(num) {
  const str = String(num);

  if (str.length <= 4) return str;

  return str.replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}

function updateCharProgress() {
  if (!charProgressDisplay || totalChars == 0) return;
  readChars = countReadCharacters();
  charsPercentage = ((readChars * 100.0) / totalChars).toFixed(2);

  previousChaptersChars = window.MOGAO_CONFIG.chapter_lengths
    .slice(0, window.MOGAO_CONFIG.chapterIndex)
    .reduce((sum, current) => sum + current, 0);
  readBookChars = readChars + previousChaptersChars;
  totalBookChars = window.MOGAO_CONFIG.chapter_lengths.reduce(
    (sum, current) => sum + current,
    0,
  );
  charsBookPercentage = ((readBookChars * 100.0) / totalBookChars).toFixed(2);

  charProgressDisplay.innerText = `${formatNumber(readChars)} / ${formatNumber(totalChars)} (${charsPercentage}%)\n${formatNumber(readBookChars)} / ${formatNumber(totalBookChars)} (${charsBookPercentage}%)`;
}

window.addEventListener("load", function () {
  countTotalCharacters();
  updateCharProgress();
  const savedPercentage = window.MOGAO_CONFIG.initialScroll;
  if (savedPercentage > 0) {
    const maxScroll = mainContent.scrollHeight - mainContent.clientHeight;
    if (maxScroll <= 0) return;
    const scrollPosition = (parseFloat(savedPercentage) / 100) * maxScroll;

    ignoreNextScroll = true;
    mainContent.scrollTo({
      top: scrollPosition,
      behavior: "auto",
    });

    setTimeout(() => {
      ignoreNextScroll = false;
    }, 50);
  }
});

let scrollTimeout;
let isResizing = false;
let scrollPercentage = 0;
let windowWidth = window.innerWidth;
mainContent.addEventListener("scroll", function () {
  if (isResizing) {
    return;
  }
  if (ignoreNextScroll) {
    ignoreNextScroll = false;
    return;
  }
  clearTimeout(scrollTimeout);
  scrollTimeout = setTimeout(function () {
    const maxScroll = mainContent.scrollHeight - mainContent.clientHeight;
    if (maxScroll <= 0) return;
    scrollPercentage = (mainContent.scrollTop / maxScroll) * 100;
    if (!isFinite(scrollPercentage)) return;

    fetch("/api/save-progress", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        book_id: window.MOGAO_CONFIG.bookId,
        chapter_index: window.MOGAO_CONFIG.chapterIndex,
        scroll_percentage: scrollPercentage,
      }),
    }).catch((err) => console.error("Failed to save progress:", err));
  }, 100);
});

let resizeTimeout;
window.addEventListener("resize", function () {
  // When rotating from horizontal to vertical, scroll was getting reset to 0.
  // This function restores the correct position while ignoring resizes caused by hiding or showing the address bar.
  let previousWidth = windowWidth;
  windowWidth = window.innerWidth;
  if (windowWidth == previousWidth) return;
  isResizing = true;
  clearTimeout(scrollTimeout);
  clearTimeout(resizeTimeout);
  resizeTimeout = setTimeout(function () {
    isResizing = false;
  }, 100);

  const maxScroll = mainContent.scrollHeight - mainContent.clientHeight;
  if (maxScroll <= 0) return;
  const scrollPosition = (scrollPercentage / 100) * maxScroll;

  ignoreNextScroll = true;
  mainContent.scrollTo({
    top: scrollPosition,
    behavior: "instant",
  });
});

window.addEventListener("beforeunload", function () {
  clearTimeout(scrollTimeout);
});

mainContent.addEventListener("scroll", updateCharProgress);
