# build_dictionary.py
import json
import server
import os


def build():
    print("Merging dictionary and frequency data...")
    output = {}

    # server.CHINESE_DICT is already loaded by importing server
    for word, entries in server.CHINESE_DICT.items():
        freq = server.CHINESE_FREQ.get(word)

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
    build()
