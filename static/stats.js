function computeStats(root) {
  return {
    known: root.querySelectorAll(".seg-known").length,
    i1: root.querySelectorAll(".seg-i1").length,
    unknown: root.querySelectorAll(".seg-unknown").length,
    oov: root.querySelectorAll(".seg-oov").length,
  };
}

function refreshStatsPopup(bookContent) {
  const statsPopup = document.getElementById("stats-popup");
  if (!statsPopup) return;

  const s = computeStats(bookContent);
  const total = s.known + s.i1 + s.unknown + s.oov;
  const pct = total ? Math.round((s.known / total) * 100) : 0;

  const title = document.createElement("div");
  title.style.cssText = "font-weight:bold;margin-bottom:4px;";
  title.textContent = "Word Coverage";

  const rows = [
    { color: "#4caf50", label: "Known", count: s.known, suffix: `(${pct}%)` },
    { color: "#64b5f6", label: "i+1 targets", count: s.i1, suffix: null },
    { color: "#ba68c8", label: "Unknown (2+)", count: s.unknown, suffix: null },
    { color: "rgba(255,90,50,0.5)", label: "OOV", count: s.oov, suffix: null },
  ];

  const rowEls = rows.map(({ color, label, count, suffix }) => {
    const div = document.createElement("div");

    const swatch = document.createElement("span");
    swatch.className = "seg-swatch";
    swatch.style.background = color;

    const bold = document.createElement("b");
    bold.textContent = count;

    div.append(swatch, `${label} `, bold);
    if (suffix) div.append(` ${suffix}`);
    return div;
  });

  statsPopup.replaceChildren(title, ...rowEls);
}

function initStatsButton(bookContent) {
  const statsBtn = document.getElementById("stats-btn");
  const statsPopup = document.getElementById("stats-popup");
  if (!statsBtn || !statsPopup || statsBtn.dataset.segInitialized) return;

  statsBtn.addEventListener("click", (event) => {
    statsPopup.classList.toggle("visible");
    event.stopPropagation();
  });

  document.addEventListener("click", (event) => {
    if (
      statsPopup.classList.contains("visible") &&
      !statsPopup.contains(event.target)
    ) {
      statsPopup.classList.remove("visible");
    }
  });

  statsBtn.dataset.segInitialized = "true";
  refreshStatsPopup(bookContent);
}
