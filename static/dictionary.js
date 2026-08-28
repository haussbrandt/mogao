let localDict = null;

const CEDICT_REFERENCE_PATTERN =
  /([^\s()[\]{}"'“”‘’<>《》〈〉「」『』【】,;:]+)\[([^\]]+)\]/gu;

window.dictionaryReady = fetch("/static/dict.json?v=3")
  .then((r) => r.json())
  .then((data) => {
    localDict = data;
    window.localDict = localDict;
    console.log("Dictionary loaded");
    const known = buildKnownSet();
    annotateBookContent(localDict, known);
    const content = document.querySelector(".book-content");
    if (content) initStatsButton(content);
  })
  .catch((err) => {
    console.error("Failed to load dictionary", err);
    throw err;
  });

function formatDictionaryFrequency(frequency) {
  if (!frequency) return null;
  if (frequency < 10000) return String(frequency);
  return frequency.toLocaleString("en-US").replace(/,/g, " ");
}

function normalizeReferencePinyin(pinyin) {
  return pinyin.normalize("NFC").replace(/\s+/g, "").toLowerCase();
}

function dictionaryResultForWord(word, pinyin = "") {
  const rawEntry = localDict?.[word];
  if (!rawEntry) return null;

  let entries = rawEntry.e.map((entry) => ({
    pinyin: entry.p,
    definitions: entry.d,
  }));
  const normalizedPinyin = normalizeReferencePinyin(pinyin);
  if (normalizedPinyin) {
    const matchingEntries = entries.filter(
      (entry) => normalizeReferencePinyin(entry.pinyin) === normalizedPinyin,
    );
    if (matchingEntries.length > 0) entries = matchingEntries;
  }

  return {
    word,
    entries,
    frequency: formatDictionaryFrequency(rawEntry.f),
    length: word.length,
    llm: !!rawEntry.llm,
  };
}

function performLookup(text) {
  if (!localDict) return [];

  const cleanText = text.replace(/\s+/g, "");
  const limit = Math.min(20, cleanText.length);
  const results = [];

  for (let i = limit; i > 0; i--) {
    const candidate = cleanText.substring(0, i);
    const result = dictionaryResultForWord(candidate);
    if (result) results.push(result);
  }
  return results;
}

function parseCedictReferences(definition) {
  const references = [];
  for (const match of definition.matchAll(CEDICT_REFERENCE_PATTERN)) {
    const writtenForm = match[1];
    const target = writtenForm.split("|").at(-1);
    references.push({
      start: match.index,
      end: match.index + match[0].length,
      raw: match[0],
      target,
      pinyin: match[2],
    });
  }
  return references;
}

function substitutableReference(definition, references) {
  if (references.length !== 1) return null;
  const reference = references[0];
  if (definition.slice(reference.end).trim()) return null;

  const prefix = definition.slice(0, reference.start).trim();
  return /(?:^|\s)(?:see|variant of)$/i.test(prefix) ? reference : null;
}

function appendReferenceAwareText(parent, definition, currentWord) {
  const references = parseCedictReferences(definition);
  let cursor = 0;

  for (const reference of references) {
    parent.appendChild(
      document.createTextNode(definition.slice(cursor, reference.start)),
    );

    if (reference.target !== currentWord && localDict?.[reference.target]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "dictionary-reference";
      button.dataset.dictionaryReference = reference.target;
      button.dataset.referencePinyin = reference.pinyin;
      button.textContent = reference.raw;
      button.setAttribute("aria-label", `Look up ${reference.target}`);
      parent.appendChild(button);
    } else {
      parent.appendChild(document.createTextNode(reference.raw));
    }
    cursor = reference.end;
  }

  parent.appendChild(document.createTextNode(definition.slice(cursor)));
  return references;
}

function appendReferencePreview(parent, result) {
  const preview = document.createElement("div");
  preview.className = "reference-preview";

  const heading = document.createElement("div");
  heading.className = "reference-preview-heading";
  const word = document.createElement("span");
  word.className = "reference-preview-word";
  word.textContent = result.word;
  heading.appendChild(word);
  preview.appendChild(heading);

  for (const entry of result.entries) {
    const pinyin = document.createElement("div");
    pinyin.className = "reference-preview-pinyin";
    pinyin.textContent = entry.pinyin;
    preview.appendChild(pinyin);

    const definitions = document.createElement("ul");
    definitions.className = "reference-preview-defs";
    for (const definition of entry.definitions) {
      const item = document.createElement("li");
      appendReferenceAwareText(item, String(definition), result.word);
      definitions.appendChild(item);
    }
    preview.appendChild(definitions);
  }
  parent.appendChild(preview);
}

function createDefinitionList(definitions, currentWord, allowPreview = true) {
  const list = document.createElement("ul");
  list.className = "entry-defs";

  for (const definitionValue of definitions) {
    const definition = String(definitionValue);
    const item = document.createElement("li");
    const references = appendReferenceAwareText(item, definition, currentWord);

    if (allowPreview) {
      const reference = substitutableReference(definition, references);
      if (reference && reference.target !== currentWord) {
        const result = dictionaryResultForWord(
          reference.target,
          reference.pinyin,
        );
        if (result) appendReferencePreview(item, result);
      }
    }
    list.appendChild(item);
  }
  return list;
}

function buildAnkiDefinitionsHtml(word, entry) {
  const list = document.createElement("ul");

  for (const definitionValue of entry.definitions) {
    const definition = String(definitionValue);
    const item = document.createElement("li");
    item.textContent = definition;

    const reference = substitutableReference(
      definition,
      parseCedictReferences(definition),
    );
    if (reference && reference.target !== word) {
      const result = dictionaryResultForWord(
        reference.target,
        reference.pinyin,
      );
      if (result) {
        const resolvedDefinitions = document.createElement("ul");
        for (const resolvedEntry of result.entries) {
          for (const resolvedDefinition of resolvedEntry.definitions) {
            const resolvedItem = document.createElement("li");
            resolvedItem.textContent = resolvedDefinition;
            resolvedDefinitions.appendChild(resolvedItem);
          }
        }
        item.appendChild(resolvedDefinitions);
      }
    }
    list.appendChild(item);
  }
  return list.outerHTML;
}

function encodeBase64Utf8(value) {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function createDictionaryPopupRenderer(container, options = {}) {
  let currentResults = [];
  let history = [];

  function render() {
    container.replaceChildren();
    container.scrollTop = 0;

    if (history.length > 0) {
      const navigation = document.createElement("div");
      navigation.className = "dictionary-navigation";
      const back = document.createElement("button");
      back.type = "button";
      back.className = "dictionary-back";
      back.textContent = "← Back";
      back.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        currentResults = history.pop();
        render();
      });
      navigation.appendChild(back);
      container.appendChild(navigation);
    }

    if (currentResults.length === 0) {
      const empty = document.createElement("div");
      empty.className = "dictionary-message";
      empty.textContent = "No definition found.";
      container.appendChild(empty);
      return;
    }

    for (const item of currentResults) {
      const resultItem = document.createElement("div");
      resultItem.className = "result-item";

      for (const entry of item.entries) {
        const head = document.createElement("div");
        head.className = "result-head";

        const word = document.createElement("span");
        word.className = "word-main";
        word.textContent = item.word;
        head.appendChild(word);

        if (item.llm) {
          const badge = document.createElement("span");
          badge.className = "freq-badge llm-badge";
          badge.textContent = "LLM";
          head.appendChild(badge);
        } else if (item.frequency) {
          const badge = document.createElement("span");
          badge.className = "freq-badge";
          badge.textContent = `#${item.frequency}`;
          head.appendChild(badge);
        }

        const action = options.createEntryAction?.({
          item,
          entry,
          definitionsHTML: buildAnkiDefinitionsHtml(item.word, entry),
        });
        if (action) head.appendChild(action);
        resultItem.appendChild(head);

        const entryBlock = document.createElement("div");
        entryBlock.className = "entry-block";
        const pinyin = document.createElement("div");
        pinyin.className = "entry-pinyin";
        pinyin.textContent = entry.pinyin;
        entryBlock.appendChild(pinyin);
        entryBlock.appendChild(createDefinitionList(entry.definitions, item.word));
        resultItem.appendChild(entryBlock);
      }
      container.appendChild(resultItem);
    }
  }

  function show(results) {
    history = [];
    currentResults = results || [];
    render();
  }

  function reset() {
    history = [];
    currentResults = [];
  }

  container.addEventListener("click", (event) => {
    const button = event.target.closest(".dictionary-reference");
    if (!button || !container.contains(button)) return;

    const result = dictionaryResultForWord(
      button.dataset.dictionaryReference,
      button.dataset.referencePinyin,
    );
    if (!result) return;

    event.preventDefault();
    event.stopPropagation();
    history.push(currentResults);
    currentResults = [result];
    render();
  });

  return { show, reset };
}
