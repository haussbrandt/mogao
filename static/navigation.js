function findAndGo(filename) {
  // The TOC usually has specific filenames e.g. "text/part001.html"
  // Sometimes it has anchors "text/part001.html#header"
  // We strip the anchor to find the page index
  const cleanFile = filename.split("#")[0];
  const anchor = filename.split("#")[1];

  const idx = window.MOGAO_CONFIG.spineMap[cleanFile];

  if (idx !== undefined) {
    let url = `/read/${window.MOGAO_CONFIG.bookId}/${idx}`;
    window.location.href = url;
  } else {
    console.log("Could not find index for", filename);
  }
}

function toggleSidebar() {
  const sidebar = document.getElementById("sidebar");
  sidebar.classList.toggle("active");

  if (sidebar.classList.contains("active")) {
    document.getElementById("sidebar-toggle").classList.remove("scroll-hidden");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const activeLink = document.querySelector("#sidebar .toc-link.active");
  if (activeLink) {
    activeLink.scrollIntoView({ block: "center", behavior: "instant" });
  }
});

// Scroll Logic (Hide/Show Button) ---
let lastScrollTop = 0;
const toggleBtn = document.getElementById("sidebar-toggle");
const sidebar = document.getElementById("sidebar");
const mainContent = document.getElementById("main");

mainContent.addEventListener(
  "scroll",
  function () {
    let scrollTop = mainContent.scrollTop;
    // If sidebar is OPEN, never hide the button (so it can be closed)
    if (sidebar.classList.contains("active")) return;
    // Don't trigger on tiny movements
    if (Math.abs(scrollTop - lastScrollTop) <= 5) return;
    if (scrollTop > lastScrollTop && scrollTop > 50) {
      // Scrolling DOWN -> Hide Button
      toggleBtn.classList.add("scroll-hidden");
    } else {
      // Scrolling UP -> Show Button
      toggleBtn.classList.remove("scroll-hidden");
    }
    lastScrollTop = scrollTop <= 0 ? 0 : scrollTop;
  },
  false,
);

// Close popup on scroll
mainContent.addEventListener("scroll", () => {
  if (isAutoScrolling) return;
  if (popup.classList.contains("visible")) closePopup();
});
