(() => {
  const REFRESH_INTERVAL_MS = 10_000;
  const refreshUrl = document.body.dataset.statusContentUrl;
  let statusContent = document.querySelector("[data-status-content]");
  const connectionMessage = document.querySelector("[data-status-connection]");
  let refreshTimer = null;
  let refreshPromise = null;
  let lastRefresh = new Date();

  if (!refreshUrl || !statusContent || !connectionMessage) return;

  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    if (!document.hidden) {
      refreshTimer = setTimeout(refreshStatus, REFRESH_INTERVAL_MS);
    }
  }

  async function refreshStatus() {
    if (document.hidden || refreshPromise) return;

    const operation = (async () => {
      const response = await fetch(refreshUrl, {
        cache: "no-store",
        signal: AbortSignal.timeout(15_000),
      });
      if (!response.ok) {
        throw new Error("Status refresh failed (" + response.status + ")");
      }

      const documentFragment = new DOMParser().parseFromString(
        await response.text(),
        "text/html",
      );
      const replacement = documentFragment.querySelector(
        "[data-status-content]",
      );
      if (!replacement) {
        throw new Error("Status refresh returned invalid content");
      }

      const scrollX = window.scrollX;
      const scrollY = window.scrollY;
      statusContent.replaceWith(replacement);
      statusContent = replacement;
      lastRefresh = new Date();
      connectionMessage.hidden = true;
      document.body.classList.remove("status-stale");
      window.scrollTo(scrollX, scrollY);
    })();
    refreshPromise = operation;

    try {
      await operation;
    } catch (error) {
      console.error(error);
      connectionMessage.textContent =
        "Unable to refresh status. Information below may be out of date. " +
        "Last updated " +
        lastRefresh.toLocaleTimeString() +
        ". Retrying automatically.";
      connectionMessage.hidden = false;
      document.body.classList.add("status-stale");
    } finally {
      if (refreshPromise === operation) refreshPromise = null;
      scheduleRefresh();
    }
  }

  document.addEventListener("visibilitychange", () => {
    clearTimeout(refreshTimer);
    if (!document.hidden) void refreshStatus();
  });

  scheduleRefresh();
})();
