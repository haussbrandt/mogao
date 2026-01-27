// Scroll Position Memory

window.addEventListener("load", function () {
  const savedPercentage = window.MOGAO_CONFIG.initialScroll;
  if (savedPercentage > 0) {
    const maxScroll = mainContent.scrollHeight - mainContent.clientHeight;
    if (maxScroll <= 0) return;
    const scrollPosition = (parseFloat(savedPercentage) / 100) * maxScroll;

    mainContent.scrollTo({
      top: scrollPosition,
      behavior: "auto",
    });
  }
});

let scrollTimeout;
mainContent.addEventListener("scroll", function () {
  clearTimeout(scrollTimeout);
  scrollTimeout = setTimeout(function () {
    const maxScroll = mainContent.scrollHeight - mainContent.clientHeight;
    if (maxScroll <= 0) return;
    const scrollPercentage = (mainContent.scrollTop / maxScroll) * 100;
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
