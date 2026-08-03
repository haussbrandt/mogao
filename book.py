# This file is heavily inspired by https://github.com/karpathy/reader3/blob/master/reader3.py
import os
import pickle
import posixpath
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import ebooklib
import nh3
from bs4 import BeautifulSoup
from ebooklib import epub

from config import settings

UNSAFE_LINK_SCHEMES = {"data", "javascript", "vbscript"}

ALLOWED_EPUB_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "div",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "rp",
    "rt",
    "ruby",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}

DISCARDED_EPUB_TAGS = {
    "audio",
    "button",
    "canvas",
    "embed",
    "form",
    "iframe",
    "input",
    "math",
    "nav",
    "noscript",
    "object",
    "script",
    "select",
    "style",
    "svg",
    "template",
    "textarea",
    "video",
}


def _filter_relative_epub_url(url: str) -> str | None:
    """Keep only application-generated relative EPUB URLs."""
    target = urlsplit(url)
    if target.netloc:
        return None
    if not target.path and target.fragment:
        return url
    if target.path.startswith("/read/"):
        return url
    if target.path.startswith("images/"):
        return url
    return None


def _filter_epub_attribute(tag: str, attribute: str, value: str) -> str | None:
    """Restrict image sources to files extracted into the book's image directory."""
    if tag != "img" or attribute != "src":
        return value

    target = urlsplit(value)
    path = PurePosixPath(target.path)
    if (
        target.scheme
        or target.netloc
        or target.query
        or target.fragment
        or len(path.parts) != 2
        or path.parts[0] != "images"
        or path.parts[1] in {"", ".", ".."}
    ):
        return None
    return value


EPUB_HTML_CLEANER = nh3.Cleaner(
    tags=ALLOWED_EPUB_TAGS,
    clean_content_tags=DISCARDED_EPUB_TAGS,
    attributes={
        "*": {"dir", "id", "lang", "title"},
        "a": {"href"},
        "img": {"alt", "height", "src", "width"},
        "li": {"value"},
        "ol": {"start"},
        "td": {"colspan", "rowspan"},
        "th": {"colspan", "rowspan"},
    },
    attribute_filter=_filter_epub_attribute,
    url_schemes={"http", "https", "mailto"},
    url_relative=_filter_relative_epub_url,
    link_rel="noopener noreferrer",
)


@dataclass
class Metadata:
    title: str
    authors: list[str]
    identifiers: list[str]

    def generate_key(self) -> uuid.UUID:
        # Keep IDs stable when unrelated metadata fields are added.
        id_source = f"{self.title}{sorted(self.authors)}{sorted(self.identifiers)}"
        return uuid.uuid5(uuid.NAMESPACE_DNS, id_source)


@dataclass
class ChapterContent:
    """
    Represents a physical file in the EPUB (Spine Item).
    A single file might contain multiple logical chapters (TOC entries).
    """

    id: str
    href: str
    title: str
    content: str
    text: str
    order: int
    chapter_characters: int


@dataclass
class TOCEntry:
    """Represents a logical entry in the navigation sidebar."""

    title: str
    href: str
    file_href: str
    anchor: str
    children: list["TOCEntry"] = field(default_factory=list)


@dataclass
class Book:
    metadata: Metadata
    spine: list[ChapterContent]
    toc: list[TOCEntry]
    images: dict[str, str]
    processed_at: str
    character_count: int = 0
    cover_image: str | None = None


def _process_metadata(ebook):
    title = ebook.get_metadata("DC", "title")
    title = title[0][0] if title else "Untitled"
    authors = ebook.get_metadata("DC", "creator")
    authors = [author[0] for author in authors] if authors else []

    identifiers = ebook.get_metadata("DC", "identifier")
    identifiers = [identifier[0] for identifier in identifiers] if identifiers else []
    return Metadata(title, authors, identifiers)


def extract_plain_text(soup: BeautifulSoup) -> str:
    """Extract clean text for LLM/Search usage."""
    text = soup.get_text(separator=" ")
    return " ".join(text.split())


def normalize_epub_path(path: str) -> str:
    """Return a decoded, archive-relative EPUB path."""
    return posixpath.normpath(unquote(path).lstrip("/"))


def rewrite_internal_links(
    soup: BeautifulSoup,
    current_href: str,
    book_id: uuid.UUID,
    spine_indices: dict[str, int],
) -> None:
    """Rewrite EPUB document links to routes understood by the reader."""
    current_path = normalize_epub_path(current_href)
    current_directory = posixpath.dirname(current_path)

    for link in soup.find_all("a", href=True):
        original_href = link.get("href")
        if not isinstance(original_href, str) or not original_href:
            continue

        try:
            target = urlsplit(original_href)
        except ValueError:
            continue

        scheme = target.scheme.lower()
        if scheme in UNSAFE_LINK_SCHEMES:
            del link["href"]
            continue
        if scheme or target.netloc:
            continue

        if target.path:
            decoded_path = unquote(target.path)
            if decoded_path.startswith("/"):
                target_path = normalize_epub_path(decoded_path)
            else:
                target_path = normalize_epub_path(
                    posixpath.join(current_directory, decoded_path)
                )
        else:
            target_path = current_path

        chapter_index = spine_indices.get(target_path)
        if chapter_index is None:
            continue

        if target_path == current_path and target.fragment and not target.query:
            link["href"] = f"#{target.fragment}"
            continue

        rewritten_href = f"/read/{book_id}/{chapter_index}"
        if target.query:
            rewritten_href += f"?{target.query}"
        if target.fragment:
            rewritten_href += f"#{target.fragment}"
        link["href"] = rewritten_href


def parse_toc_recursive(toc_list):
    """
    Recursively parses the TOC structure from ebooklib.
    """
    result = []

    for item in toc_list:
        # ebooklib TOC items are either `Link` objects or tuples (Section, [Children])
        if isinstance(item, tuple):
            section, children = item
            entry = TOCEntry(
                title=section.title,
                href=section.href,
                file_href=section.href.split("#")[0],
                anchor=section.href.split("#")[1] if "#" in section.href else "",
                children=parse_toc_recursive(children),
            )
            result.append(entry)
        elif isinstance(item, epub.Link):
            entry = TOCEntry(
                title=item.title,
                href=item.href,
                file_href=item.href.split("#")[0],
                anchor=item.href.split("#")[1] if "#" in item.href else "",
            )
            result.append(entry)
        # Note: ebooklib sometimes returns direct Section objects without children
        elif isinstance(item, epub.Section):
            entry = TOCEntry(
                title=item.title,
                href=item.href,
                file_href=item.href.split("#")[0],
                anchor=item.href.split("#")[1] if "#" in item.href else "",
            )
            result.append(entry)

    return result


def get_fallback_toc(book_obj):
    """
    If TOC is missing, build a flat one from the Spine.
    """
    toc = []
    for item in book_obj.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            name = item.get_name()
            title = (
                item.get_name()
                .replace(".html", "")
                .replace(".xhtml", "")
                .replace("_", " ")
                .title()
            )
            toc.append(TOCEntry(title=title, href=name, file_href=name, anchor=""))
    return toc


def generate_book(path, library_dir) -> Book:
    ebook = epub.read_epub(path)
    metadata = _process_metadata(ebook)
    unique_key = metadata.generate_key()

    # TODO: Ask before replacing an existing book.
    os.makedirs(library_dir, exist_ok=True)
    output_dir = f"{library_dir}/{unique_key}"
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    images_dir = os.path.join(output_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    image_map: dict[str, str] = {}
    cover_image_filename = None
    for item in ebook.get_items():
        if item.get_type() in (ebooklib.ITEM_IMAGE, ebooklib.ITEM_COVER):
            original_fname = os.path.basename(item.get_name())
            safe_fname = "".join(
                [c for c in original_fname if c.isalpha() or c.isdigit() or c in "._-"]
            ).strip()

            if (
                item.get_type() == ebooklib.ITEM_COVER
                or "cover" in safe_fname
                or item.id == "cover-image"
            ):
                cover_image_filename = safe_fname

            local_path = os.path.join(images_dir, safe_fname)
            with open(local_path, "wb") as f:
                f.write(item.get_content())

            # EPUBs are inconsistent about using full image paths or basenames.
            rel_path = f"images/{safe_fname}"
            image_map[item.get_name()] = rel_path
            image_map[original_fname] = rel_path

    toc_structure = parse_toc_recursive(ebook.toc)
    if not toc_structure:
        toc_structure = get_fallback_toc(ebook)

    total_characters = 0
    spine_chapters = []
    spine_indices: dict[str, int] = {}
    for item_id, _ in ebook.spine:
        item = ebook.get_item_with_id(item_id)
        if item and item.get_type() == ebooklib.ITEM_DOCUMENT:
            spine_indices[normalize_epub_path(item.get_name())] = len(spine_indices)

    for spine_item in ebook.spine:
        item_id, _linear = spine_item
        item = ebook.get_item_with_id(item_id)

        if not item:
            continue

        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            raw_content = item.get_content().decode("utf-8", errors="ignore")
            soup = BeautifulSoup(raw_content, "html.parser")

            for img in soup.find_all(["img", "image"]):
                src = img.get("src") or img.get("xlink:href") or img.get("href")
                if not src:
                    continue

                src_decoded = unquote(src)
                filename = os.path.basename(src_decoded)

                new_src = None
                if src_decoded in image_map:
                    new_src = image_map[src_decoded]
                elif filename in image_map:
                    new_src = image_map[filename]

                if new_src:
                    if img.name == "img":
                        img["src"] = new_src
                    elif img.has_attr("xlink:href"):
                        img["xlink:href"] = new_src
                    else:
                        img["href"] = new_src

            rewrite_internal_links(
                soup,
                item.get_name(),
                unique_key,
                spine_indices,
            )

            body = soup.find("body")
            if body:
                html_fragment = "".join(str(child) for child in body.contents)
            else:
                html_fragment = str(soup)

            final_html = EPUB_HTML_CLEANER.clean(html_fragment)
            clean_soup = BeautifulSoup(final_html, "html.parser")
            chapter_text = extract_plain_text(clean_soup)
            chapter_characters = sum(
                1 for c in chapter_text if "\u4e00" <= c <= "\u9fff"
            )
            total_characters += chapter_characters
            chapter_index = spine_indices[normalize_epub_path(item.get_name())]
            chapter = ChapterContent(
                id=item_id,
                href=item.get_name(),
                title=f"Section {chapter_index + 1}",
                content=final_html,
                text=chapter_text,
                order=chapter_index,
                chapter_characters=chapter_characters,
            )
            spine_chapters.append(chapter)

    processed_book = Book(
        metadata,
        spine_chapters,
        toc_structure,
        image_map,
        datetime.now().isoformat(),
        character_count=total_characters,
        cover_image=cover_image_filename,
    )

    p_path = os.path.join(output_dir, "book.pkl")
    with open(p_path, "wb") as f:
        pickle.dump(processed_book, f)

    return processed_book


if __name__ == "__main__":
    for path in Path("samples").glob("**/*.epub"):
        book = generate_book(path, settings.paths.library)
