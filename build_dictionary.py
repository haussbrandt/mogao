from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import logging
import os
import re
import tempfile
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

from logging_config import configure_logging


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = PROJECT_ROOT / "static" / "dict.json"

# CC-CEDICT's verified MDBG download forbids scripted access. The project
# editor provides this machine-readable, non-verified export under the same
# CC BY-SA 4.0 license.
DEFAULT_CEDICT_URL = (
    "https://cc-cedict.org/editor/editor_export_cedict.php?c=zip"
)
DEFAULT_BCC_URL = (
    "https://bcc.blcu.edu.cn/api/datasets/"
    "multi_domain_total_word_freq.txt/download"
)
DEFAULT_SUBTLEX_URL = "https://doi.org/10.1371/journal.pone.0010729.s002"

DOWNLOAD_TIMEOUT_SECONDS = 180
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
USER_AGENT = "Mogao dictionary builder"

PINYIN_SYLLABLE_RE = re.compile(r"([a-zA-Zü:]+)([1-5])")
CEDICT_ENTRY_RE = re.compile(
    r"(\S+)\s+(\S+)\s+\[\[?(.*?)\]\]?\s+/(.*)/"
)

configure_logging()
logger = logging.getLogger(__name__)


def _tone_marked_syllable(match: re.Match[str]) -> str:
    tone_map = {
        "a": "āáǎàa",
        "e": "ēéěèe",
        "i": "īíǐìi",
        "o": "ōóǒòo",
        "u": "ūúǔùu",
        "v": "ǖǘǚǜü",
        "ü": "ǖǘǚǜü",
    }
    base, tone = match.groups()
    tone_index = int(tone) - 1
    base = base.lower().replace("u:", "ü").replace("v", "ü")

    marked_index = -1
    for character in ("a", "e", "o"):
        if character in base:
            marked_index = base.find(character)
            break
    if marked_index == -1:
        for index in range(len(base) - 1, -1, -1):
            if base[index] in "iuü":
                marked_index = index
                break

    if marked_index != -1 and tone_index < 4:
        character = base[marked_index]
        base = (
            base[:marked_index]
            + tone_map[character][tone_index]
            + base[marked_index + 1 :]
        )
    return base


def convert_pinyin_tone(pinyin: str) -> str:
    return PINYIN_SYLLABLE_RE.sub(_tone_marked_syllable, pinyin).replace(" ", "")


def read_source(source: str, description: str) -> bytes:
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme in {"http", "https"}:
        logger.info("Downloading %s", description)
        request = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(
            request, timeout=DOWNLOAD_TIMEOUT_SECONDS
        ) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
                raise ValueError(
                    f"{description} is larger than the "
                    f"{MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB limit"
                )
            data = response.read(MAX_DOWNLOAD_BYTES + 1)
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise ValueError(
                f"{description} is larger than the "
                f"{MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB limit"
            )
        return data

    path = Path(source).expanduser()
    logger.info("Reading %s from %s", description, path)
    return path.read_bytes()


def _zip_members(data: bytes) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return [
            (member.filename, archive.read(member))
            for member in archive.infolist()
            if not member.is_dir()
        ]


def _select_archive_member(
    data: bytes,
    *,
    preferred_names: tuple[str, ...],
    description: str,
) -> tuple[str, bytes]:
    members = _zip_members(data)
    for preferred_name in preferred_names:
        for name, content in members:
            if Path(name).name.lower() == preferred_name.lower():
                return name, content
    if len(members) == 1:
        return members[0]
    available = ", ".join(name for name, _ in members)
    raise ValueError(
        f"Could not identify {description} in archive; found: {available}"
    )


def cedict_text(data: bytes) -> str:
    if data.startswith(b"PK\x03\x04"):
        name, data = _select_archive_member(
            data,
            preferred_names=(
                "cedict_ts.u8",
                "cedict_1_0_ts_utf-8_mdbg.txt",
            ),
            description="a CC-CEDICT text file",
        )
        logger.info("Using CC-CEDICT archive member %s", name)
    elif data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)
    return data.decode("utf-8-sig")


def load_dictionary(source: str) -> dict[str, list[dict[str, object]]]:
    logger.info("Loading CC-CEDICT")
    dictionary: dict[str, list[dict[str, object]]] = {}
    skipped_lines = 0

    for line in cedict_text(read_source(source, "CC-CEDICT")).splitlines():
        if line.startswith("#") or not line.strip():
            continue
        match = CEDICT_ENTRY_RE.fullmatch(line)
        if not match:
            skipped_lines += 1
            continue

        _traditional, simplified, pinyin_raw, definitions_raw = match.groups()
        definitions = [
            PINYIN_SYLLABLE_RE.sub(_tone_marked_syllable, definition)
            for definition in definitions_raw.split("/")
        ]
        entry = {
            "pinyin": convert_pinyin_tone(pinyin_raw),
            "definitions": definitions,
        }

        word_entries = dictionary.setdefault(simplified, [])
        for existing_entry in word_entries:
            if existing_entry["pinyin"] == entry["pinyin"]:
                existing_definitions = existing_entry["definitions"]
                assert isinstance(existing_definitions, list)
                existing_definitions.extend(definitions)
                break
        else:
            word_entries.append(entry)

    if skipped_lines:
        logger.warning("Skipped %d unrecognized CC-CEDICT lines", skipped_lines)
    logger.info("CC-CEDICT loaded: %d simplified terms", len(dictionary))
    return dictionary


def _rank_counts(counts: dict[str, int]) -> dict[str, int]:
    ranked: dict[str, int] = {}
    previous_count: int | None = None
    previous_rank = 0
    for position, (term, count) in enumerate(
        sorted(counts.items(), key=lambda item: (-item[1], item[0])), start=1
    ):
        if count != previous_count:
            previous_rank = position
            previous_count = count
        ranked[term] = previous_rank
    return ranked


def load_bcc_frequency(source: str, valid_terms: set[str]) -> dict[str, int]:
    logger.info("Loading BCC multi-domain word frequencies")
    data = read_source(source, "BCC frequencies")
    if data.startswith(b"PK\x03\x04"):
        name, data = _select_archive_member(
            data,
            preferred_names=("multi_domain_total_word_freq.txt",),
            description="the BCC word-frequency CSV",
        )
        logger.info("Using BCC archive member %s", name)

    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    if reader.fieldnames is None or not {"token", "count"}.issubset(
        reader.fieldnames
    ):
        raise ValueError("BCC data must contain token and count columns")

    counts: dict[str, int] = {}
    for row in reader:
        term = row["token"]
        if term not in valid_terms:
            continue
        try:
            count = int(row["count"])
        except (TypeError, ValueError):
            continue
        if count > 0:
            counts[term] = max(count, counts.get(term, 0))

    ranks = _rank_counts(counts)
    logger.info("BCC frequencies loaded: %d dictionary terms", len(ranks))
    return ranks


def load_subtlex_frequency(source: str, valid_terms: set[str]) -> dict[str, int]:
    logger.info("Loading SUBTLEX-CH word frequencies")
    data = read_source(source, "SUBTLEX-CH frequencies")
    if data.startswith(b"PK\x03\x04"):
        name, data = _select_archive_member(
            data,
            preferred_names=("SUBTLEX-CH-WF",),
            description="the SUBTLEX-CH word-frequency table",
        )
        logger.info("Using SUBTLEX-CH archive member %s", name)

    rows = csv.reader(io.StringIO(data.decode("gb18030")), delimiter="\t")
    try:
        next(rows)
        next(rows)
        header = next(rows)
    except StopIteration as error:
        raise ValueError("SUBTLEX-CH table is missing its header") from error

    try:
        word_index = header.index("Word")
        count_index = header.index("WCount")
    except ValueError as error:
        raise ValueError(
            "SUBTLEX-CH data must contain Word and WCount columns"
        ) from error

    counts: dict[str, int] = {}
    for row in rows:
        if len(row) <= max(word_index, count_index):
            continue
        term = row[word_index]
        if term not in valid_terms:
            continue
        try:
            count = int(row[count_index])
        except ValueError:
            continue
        if count > 0:
            counts[term] = max(count, counts.get(term, 0))

    ranks = _rank_counts(counts)
    logger.info("SUBTLEX-CH frequencies loaded: %d dictionary terms", len(ranks))
    return ranks


def _load_yomitan_json(data: bytes, valid_terms: set[str]) -> dict[str, float]:
    entries = json.loads(data.decode("utf-8-sig"))
    ranks: dict[str, float] = {}
    for entry in entries:
        if not isinstance(entry, list) or len(entry) < 3 or entry[1] != "freq":
            continue
        term, value = entry[0], entry[2]
        if (
            term in valid_terms
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value > 0
        ):
            ranks[term] = min(float(value), ranks.get(term, float("inf")))
    return ranks


def load_yomitan_frequency(source: str, valid_terms: set[str]) -> dict[str, float]:
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme not in {"http", "https"}:
        path = Path(source).expanduser()
        if path.is_dir():
            ranks: dict[str, float] = {}
            files = sorted(path.rglob("*term_meta_bank*.json"))
            if not files:
                raise ValueError(f"No Yomitan term metadata banks found in {path}")
            for file_path in files:
                for term, rank in _load_yomitan_json(
                    file_path.read_bytes(), valid_terms
                ).items():
                    ranks[term] = min(rank, ranks.get(term, float("inf")))
            logger.info("Custom frequencies loaded: %d dictionary terms", len(ranks))
            return ranks

    data = read_source(source, "custom Yomitan frequencies")
    if data.startswith(b"PK\x03\x04"):
        ranks: dict[str, float] = {}
        members = [
            (name, content)
            for name, content in _zip_members(data)
            if "term_meta_bank" in Path(name).name and name.endswith(".json")
        ]
        if not members:
            raise ValueError("No Yomitan term metadata banks found in archive")
        for _name, content in members:
            for term, rank in _load_yomitan_json(content, valid_terms).items():
                ranks[term] = min(rank, ranks.get(term, float("inf")))
    else:
        ranks = _load_yomitan_json(data, valid_terms)
    logger.info("Custom frequencies loaded: %d dictionary terms", len(ranks))
    return ranks


def _normalized_ranks(scores: dict[str, int | float]) -> dict[str, float]:
    if not scores:
        return {}

    normalized: dict[str, float] = {}
    previous_score: int | float | None = None
    previous_rank = 0
    ordered = sorted(scores.items(), key=lambda item: (item[1], item[0]))
    total = len(ordered)
    for position, (term, score) in enumerate(ordered, start=1):
        if score != previous_score:
            previous_rank = position
            previous_score = score
        normalized[term] = previous_rank / total
    return normalized


def combine_frequency_ranks(
    sources: list[dict[str, int | float]],
) -> dict[str, int]:
    """Normalize source ranks, take their harmonic mean, then rerank terms."""
    scores_by_term: defaultdict[str, list[float]] = defaultdict(list)
    for source in sources:
        for term, rank in _normalized_ranks(source).items():
            scores_by_term[term].append(rank)

    combined_scores = {
        term: len(scores) / sum(1.0 / score for score in scores)
        for term, scores in scores_by_term.items()
    }
    combined_ranks: dict[str, int] = {}
    previous_score: float | None = None
    previous_rank = 0
    for position, (term, score) in enumerate(
        sorted(combined_scores.items(), key=lambda item: (item[1], item[0])),
        start=1,
    ):
        if score != previous_score:
            previous_rank = position
            previous_score = score
        combined_ranks[term] = previous_rank
    logger.info("Combined frequencies: %d dictionary terms", len(combined_ranks))
    return combined_ranks


def build_output(
    dictionary: dict[str, list[dict[str, object]]],
    frequencies: dict[str, int],
) -> dict[str, dict[str, object]]:
    output: dict[str, dict[str, object]] = {}
    for word, entries in dictionary.items():
        compact_entries = [
            {"p": entry["pinyin"], "d": entry["definitions"]}
            for entry in entries
        ]
        output[word] = {"e": compact_entries, "f": frequencies.get(word)}
    return output


def save_output(output: dict[str, dict[str, object]], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as output_file:
            json.dump(
                output,
                output_file,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    logger.info(
        "Saved %d terms to %s (%.1f MB)",
        len(output),
        destination,
        destination.stat().st_size / (1024 * 1024),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build Mogao's popup dictionary from CC-CEDICT and frequency data. "
            "Sources may be URLs or local files."
        )
    )
    parser.add_argument(
        "--cedict",
        default=DEFAULT_CEDICT_URL,
        metavar="SOURCE",
        help="CC-CEDICT text, gzip, or ZIP source",
    )
    parser.add_argument(
        "--bcc",
        default=DEFAULT_BCC_URL,
        metavar="SOURCE",
        help="BCC multi-domain CSV or ZIP source",
    )
    parser.add_argument(
        "--subtlex",
        default=DEFAULT_SUBTLEX_URL,
        metavar="SOURCE",
        help="SUBTLEX-CH-WF file or ZIP source",
    )
    parser.add_argument(
        "--without-bcc",
        action="store_true",
        help="exclude the default BCC frequency source",
    )
    parser.add_argument(
        "--without-subtlex",
        action="store_true",
        help="exclude the default SUBTLEX-CH frequency source",
    )
    parser.add_argument(
        "--frequency",
        action="append",
        default=[],
        metavar="SOURCE",
        help=(
            "add a Yomitan term-meta JSON file, directory, or ZIP; "
            "may be repeated"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"output file (default: {DEFAULT_OUTPUT})",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    dictionary = load_dictionary(args.cedict)
    valid_terms = set(dictionary)

    frequency_sources: list[dict[str, int | float]] = []
    if not args.without_bcc:
        frequency_sources.append(load_bcc_frequency(args.bcc, valid_terms))
    if not args.without_subtlex:
        frequency_sources.append(load_subtlex_frequency(args.subtlex, valid_terms))
    frequency_sources.extend(
        load_yomitan_frequency(source, valid_terms) for source in args.frequency
    )

    frequencies = combine_frequency_ranks(frequency_sources)
    save_output(build_output(dictionary, frequencies), args.output.resolve())


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        logger.error("Dictionary build cancelled")
        raise SystemExit(1)
