/**
 * Basically a copy of book_dict.js with very little changed.
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
   * dictionary.
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
      const resp = await fetch(
        `${window.MOGAO_CONFIG.videoBasePath}/api/video-dict/${videoId}`,
      );
      if (!resp.ok) return null;
      data = await resp.json();
    } catch (e) {
      console.warn("[video_dict] fetch error:", e);
      return null;
    }

    const added = mergeWords(data.words);

    // If we added new words and segmentation is running, re-annotate so the
    // newly recognised words get coloured correctly right away.
    if (added > 0 && typeof window.reannotateWithNewDict === "function") {
      window.reannotateWithNewDict(window.localDict);
    }

    return data;
  }

  try {
    await window.dictionaryReady;
  } catch {
    return;
  }
  let data = await fetchAndMerge();

  // Poll while still processing (server is working through chunks in the background)
  if (data?.status === "processing") {
    const pollId = setInterval(async () => {
      data = await fetchAndMerge();
      if (data?.status !== "processing") clearInterval(pollId);
    }, POLL_INTERVAL_MS);
  }
})();
