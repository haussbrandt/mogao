let localDict = null;

fetch("/static/dict.json?v=2")
  .then((r) => r.json())
  .then((data) => {
    localDict = data;
    console.log("Dictionary loaded");
    const known = buildKnownSet();
    annotateBookContent(localDict, known);
    initStatsButton(bookContent);
  })
  .catch((err) => console.error("Failed to load dictionary", err));

function performLookup(text) {
  if (!localDict) return [];

  const cleanText = text.replace(/\s+/g, "");
  const limit = Math.min(6, cleanText.length);
  const results = [];

  for (let i = limit; i > 0; i--) {
    const candidate = cleanText.substring(0, i);
    const entry = localDict[candidate];

    if (entry) {
      // Expand the minified JSON back to usable structure
      let freqStr = null;
      if (entry.f) {
        if (entry.f < 10000) freqStr = "" + entry.f;
        else freqStr = entry.f.toLocaleString("en-US").replace(/,/g, " ");
      }

      results.push({
        word: candidate,
        entries: entry.e.map((e) => ({ pinyin: e.p, definitions: e.d })),
        frequency: freqStr,
        length: i,
      });
    }
  }
  return results;
}
