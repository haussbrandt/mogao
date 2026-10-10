// Index only the user's words; dictionary entries are shared with lookup.
function createVocabularySearch() {
  const cache = new Map();
  const normalize = (text) => text.toLowerCase().normalize("NFKD").replace(/\p{M}/gu, "");
  const tokens = (text) => text.match(/[\p{L}\p{N}]+/gu) || [];
  const pinyin = (text) => normalize(text).replace(/u:|v/g, "u").replace(/[1-5\s'’\-]/g, "");

  function index(word, entry) {
    const saved = cache.get(word);
    if (saved && saved.entry === entry) return saved;
    const readings = (entry?.e || []).map((e) => pinyin(e.p || ""));
    const definitions = normalize((entry?.e || []).flatMap((e) => e.d || []).join(" "));
    const result = {
      entry, word: normalize(word), readings, definitions,
      terms: [...new Set([normalize(word), ...readings, ...tokens(definitions)])],
    };
    cache.set(word, result);
    return result;
  }

  // Bounded edit distance, including adjacent transpositions (e.g. "wrold").
  function closeEnough(a, b) {
    const max = a.length >= 7 ? 2 : a.length >= 3 || /\p{Script=Han}{2}/u.test(a) ? 1 : 0;
    if (!max || Math.abs(a.length - b.length) > max) return false;
    let previous = Array.from({ length: b.length + 1 }, (_, i) => i);
    let beforePrevious;
    for (let i = 1; i <= a.length; i++) {
      const row = [i];
      for (let j = 1; j <= b.length; j++) {
        row[j] = Math.min(row[j - 1] + 1, previous[j] + 1,
          previous[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
        if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) {
          row[j] = Math.min(row[j], beforePrevious[j - 2] + 1);
        }
      }
      if (Math.min(...row) > max) return false;
      beforePrevious = previous;
      previous = row;
    }
    return previous[b.length] <= max;
  }

  return (words, query, dictionary = {}) => {
    query = normalize(query.trim());
    const sorted = [...words].sort((a, b) => a.localeCompare(b, "zh"));
    if (!query) return sorted;
    const parts = tokens(query);
    const reading = pinyin(query);
    function rank(word) {
      const item = index(word, dictionary[word]);
      if (item.word.includes(query)) return 0;
      if (reading && item.readings.some((r) => r.includes(reading))) return 1;
      if (item.definitions.includes(query)) return 2;
      if (reading && item.readings.some((r) => closeEnough(reading, r))) return 4;
      if (!parts.length) return Infinity;
      let fuzzy = false;
      for (const part of parts) {
        if (item.terms.some((term) => term.includes(part))) continue;
        if (!item.terms.some((term) => closeEnough(part, term))) return Infinity;
        fuzzy = true;
      }
      return fuzzy ? 4 : 3;
    }
    return sorted.map((word) => ({ word, rank: rank(word) }))
      .filter((item) => Number.isFinite(item.rank))
      .sort((a, b) => a.rank - b.rank).map((item) => item.word);
  };
}
