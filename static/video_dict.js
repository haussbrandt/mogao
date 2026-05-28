/**
 * Basically a copy of book_dict.json with very little chagned.
 * TODO: Refactor
 */

(async function () {
  "use strict";

  const { videoId } = window.MOGAO_CONFIG;
  const POLL_INTERVAL_MS = 30_000;

  function toLocalEntry(raw) {
    return {
      f: 100,
      e: (raw.e || []).map((e) => ({
        p: e.p || "",
        d: e.d || [],
      })),
      llm: true,
    };
  }

  /**
   * Merge server words into window.localDict.
   * We only add; we never overwrite an entry that already came from the main
   * dictionary (those have richer data and user-verified frequency ranks).
   * Returns the number of newly added words.
   */
  function mergeWords(words) {
    if (!window.localDict || !words) return 0;
    let added = 0;
    for (const [word, raw] of Object.entries(words)) {
      if (!window.localDict[word]) {
        window.localDict[word] = toLocalEntry(raw);
        added++;
      }
    }
    return added;
  }

  async function fetchAndMerge() {
    let data = null;
    try {
      const resp = await fetch(`/api/video-dict/${videoId}`);
      if (!resp.ok) return null;
      data = await resp.json();
    } catch (e) {
      console.warn("[video_dict] fetch error:", e);
      return null;
    }

    const added = mergeWords(data.words);

    // If we added new words and segmentation is running, re-annotate so the
    // newly recognised proper nouns get coloured correctly right away.
    if (added > 0 && typeof window.reannotateWithNewDict === "function") {
      window.reannotateWithNewDict(window.localDict);
    }

    return data;
  }

  // ── Entry point: wait for dictionary.js to finish loading localDict ─────────
  await new Promise((resolve) => {
    // dictionary.js is synchronous or near-synchronous — give it one tick first
    if (window.localDict) {
      return resolve();
    }
    const id = setInterval(() => {
      if (window.localDict) {
        clearInterval(id);
        resolve();
      }
    }, 100);
  });

  let data = await fetchAndMerge();
  console.log(data);

  // Poll while still processing (server is working through chunks in the background)
  if (data?.status === "processing") {
    const pollId = setInterval(async () => {
      data = await fetchAndMerge();
      if (data?.status !== "processing") clearInterval(pollId);
    }, POLL_INTERVAL_MS);
  }
})();
