# build_dictionary.py
import glob
import json
import os
import re

from config import settings

CHINESE_DICT = {}
CHINESE_FREQ = {}


def convert_pinyin_tone(pinyin_str):
    tone_map = {
        "a": "āáǎàa",
        "e": "ēéěèe",
        "i": "īíǐìi",
        "o": "ōóǒòo",
        "u": "ūúǔùu",
        "v": "ǖǘǚǜü",
        "ü": "ǖǘǚǜü",
    }
    results = []
    for word in pinyin_str.split():
        match = re.match(r"^([a-zA-Zü:]+)([1-5])$", word)
        if not match:
            results.append(word)
            continue
        base, tone = match.groups()
        tone_idx = int(tone) - 1
        base = base.replace("u:", "ü").replace("v", "ü")
        idx = -1
        for char in ["a", "e", "o"]:
            if char in base:
                idx = base.find(char)
                break
        if idx == -1:
            for i in range(len(base) - 1, -1, -1):
                if base[i] in "iuü":
                    idx = i
                    break
        if idx != -1 and tone_idx < 4:
            char = base[idx]
            replacement = tone_map[char][tone_idx]
            base = base[:idx] + replacement + base[idx + 1 :]
        results.append(base)
    return "".join(results)


def load_dictionary():
    global CHINESE_DICT
    if not os.path.exists(settings.paths.dictionary):
        print(f"Warning: {settings.paths.dictionary} not found.")
        return

    print("Loading dictionary...")
    pattern = re.compile(r"(\S+)\s+(\S+)\s+\[(.*?)\]\s+/(.*)/")

    with open(settings.paths.dictionary, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            match = pattern.match(line)
            if match:
                trad, simp, pinyin_raw, defs_str = match.groups()

                # TODO: I think it's overcomplicated but it works, so I leave it for now
                pinyin_regex = re.compile(r"([a-zA-Zü:]+)([1-5])")
                definitions = []
                for definition in defs_str.split("/"):
                    processed_definition = pinyin_regex.sub(
                        lambda m: convert_pinyin_tone(m.group(0).lower()), definition
                    )
                    definitions.append(processed_definition)
                entry = {
                    "pinyin": convert_pinyin_tone(pinyin_raw.lower()),
                    "definitions": definitions,
                }
                if simp not in CHINESE_DICT:
                    CHINESE_DICT[simp] = []
                entry_exists = False
                for dict_entry in CHINESE_DICT[simp]:
                    if dict_entry["pinyin"] == entry["pinyin"]:
                        dict_entry["definitions"].extend(entry["definitions"])
                        entry_exists = True
                        break
                if not entry_exists:
                    CHINESE_DICT[simp].append(entry)
    from pprint import pprint

    pprint(CHINESE_DICT["黑"])
    pprint(CHINESE_DICT["了"])
    print(f"Dictionary loaded: {len(CHINESE_DICT)} entries.")


def load_frequency():
    """
    Scans FREQ_DIR for Yomitan-formatted JSON files.
    Calculates the Harmonic Mean of ranks across all files.
    """
    global CHINESE_FREQ
    if not os.path.exists(settings.paths.frequencies):
        print(f"Warning: {settings.paths.frequencies} directory not found.")
        return

    print("Loading frequency data (this might take a moment)...")

    temp_scores = {}  # word -> [score1, score2, ...]

    files = glob.glob(
        os.path.join(settings.paths.frequencies, "**", "*term_meta_bank*.json"),
        recursive=True,
    )

    if not files:
        print("No frequency JSON files found in freqs/ folder.")
        return

    for file_path in files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            for entry in data:
                if isinstance(entry, list) and len(entry) >= 3 and entry[1] == "freq":
                    term = entry[0]
                    val = entry[2]

                    if isinstance(val, (int, float)) and val > 0:
                        if term not in temp_scores:
                            temp_scores[term] = []
                        temp_scores[term].append(val)
        except Exception as e:
            print(f"Error reading {file_path}: {e}")

    # Calculate Harmonic Mean
    count = 0
    for term, scores in temp_scores.items():
        try:
            reciprocal_sum = sum(1.0 / s for s in scores)
            if reciprocal_sum > 0:
                hm = len(scores) / reciprocal_sum
                CHINESE_FREQ[term] = int(hm)
                count += 1
        except Exception:
            pass

    print(f"Frequency data loaded: {count} unique terms.")


def build():
    print("Merging dictionary and frequency data...")
    output = {}

    # server.CHINESE_DICT is already loaded by importing server
    for word, entries in CHINESE_DICT.items():
        freq = CHINESE_FREQ.get(word)

        # Minify keys to save space
        compact_entries = []
        for entry in entries:
            compact_entries.append({"p": entry["pinyin"], "d": entry["definitions"]})

        output[word] = {"e": compact_entries, "f": freq}

    print(f"Total words: {len(output)}")
    print("Saving to static/dict.json...")

    # Ensure static dir exists
    os.makedirs("static", exist_ok=True)

    with open("static/dict.json", "w", encoding="utf-8") as f:
        # separators=(',', ':') removes whitespace to minimize size
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

    print("Done! You can now restart the server.")


if __name__ == "__main__":
    load_dictionary()
    load_frequency()
    build()
