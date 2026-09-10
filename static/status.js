(() => {
  const REFRESH_INTERVAL_MS = 10_000;
  const ACTIVE_REFRESH_INTERVAL_MS = 2_000;
  const refreshUrl = document.body.dataset.statusContentUrl;
  let statusContent = document.querySelector("[data-status-content]");
  const connectionMessage = document.querySelector("[data-status-connection]");
  const actionMessage = document.querySelector("[data-status-action-error]");
  let refreshTimer = null;
  let refreshPromise = null;
  let lastRefresh = new Date();

  if (!refreshUrl || !statusContent || !connectionMessage) return;

  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    if (!document.hidden) {
      const delay =
        statusContent.dataset.backgroundWorkActive === "true"
          ? ACTIVE_REFRESH_INTERVAL_MS
          : REFRESH_INTERVAL_MS;
      refreshTimer = setTimeout(refreshStatus, delay);
    }
  }

  async function submitStatusAction(form) {
    const response = await fetch(form.action, {
      method: form.method,
      headers: { "X-Requested-With": "status-page" },
      signal: AbortSignal.timeout(15_000),
    });
    if (response.ok) return;

    let message = `Request failed (${response.status})`;
    try {
      const payload = await response.json();
      if (typeof payload.detail === "string") message = payload.detail;
    } catch {
      // Proxy errors and other non-JSON responses use the status message.
    }
    throw new Error(message);
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

  document.addEventListener("submit", async (event) => {
    const form = event.target.closest("form[data-status-action]");
    if (!form) return;

    event.preventDefault();
    clearTimeout(refreshTimer);
    const submitter = event.submitter;
    if (submitter) submitter.disabled = true;
    actionMessage.hidden = true;

    try {
      await submitStatusAction(form);
      await refreshStatus();
    } catch (error) {
      console.error(error);
      const actionLabel = form.dataset.actionLabel || "complete action";
      actionMessage.textContent = `Could not ${actionLabel}: ${error.message}`;
      actionMessage.hidden = false;
      scheduleRefresh();
    }
  });

  scheduleRefresh();
})();
