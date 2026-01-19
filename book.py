# This file is heavily inspired by https://github.com/karpathy/reader3/blob/master/reader3.py
from ast import Dict
import os
import pickle
import shutil
import uuid

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, Comment


@dataclass
class Metadata:
    title: str
    authors: list[str]
    identifiers: list[str]

    def generate_key(self) -> uuid.UUID:
        # I want to have a stable way of generating a unique id for each book.
        # It's possible to directly use str(self), but if any new fields are added in the future
        # it would change all of the IDs.
        id_source = f"{self.title}{sorted(self.authors)}{sorted(self.identifiers)}"
        return uuid.uuid5(uuid.NAMESPACE_DNS, id_source)


@dataclass
class ChapterContent:
    """
    Represents a physical file in the EPUB (Spine Item).
    A single file might contain multiple logical chapters (TOC entries).
    """

    id: str  # Internal ID (e.g., 'item_1')
    href: str  # Filename (e.g., 'part01.html')
    title: str  # Best guess title from file
    content: str  # Cleaned HTML with rewritten image paths
    text: str  # Plain text for search/translation
    order: int  # Linear reading order


@dataclass
class TOCEntry:
    """Represents a logical entry in the navigation sidebar."""

    title: str
    href: str  # original href (e.g., 'part01.html#chapter1')
    file_href: str  # just the filename (e.g., 'part01.html')
    anchor: str  # just the anchor (e.g., 'chapter1'), empty if none
    children: list["TOCEntry"] = field(default_factory=list)


@dataclass
class Book:
    metadata: Metadata
    spine: list[ChapterContent]
    toc: list[TOCEntry]
    images: dict[str, str]
    processed_at: str


def _process_metadata(ebook):
    title = ebook.get_metadata("DC", "title")
    title = title[0][0] if title else "Untitled"
    authors = ebook.get_metadata("DC", "creator")
    authors = [author[0] for author in authors] if authors else []

    identifiers = ebook.get_metadata("DC", "identifier")
    identifiers = [identifier[0] for identifier in identifiers] if identifiers else []
    return Metadata(title, authors, identifiers)


def clean_html_content(soup: BeautifulSoup) -> BeautifulSoup:
    # Remove dangerous/useless tags
    for tag in soup(["script", "style", "iframe", "video", "nav", "form", "button"]):
        tag.decompose()

    # Remove HTML comments
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    # Remove input tags
    for tag in soup.find_all("input"):
        tag.decompose()

    return soup


def extract_plain_text(soup: BeautifulSoup) -> str:
    """Extract clean text for LLM/Search usage."""
    text = soup.get_text(separator=" ")
    # Collapse whitespace
    return " ".join(text.split())


def parse_toc_recursive(toc_list, depth=0):
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
                children=parse_toc_recursive(children, depth + 1),
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
            # Try to guess a title from the content or ID
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

    # Prepare output directories
    # TODO: Ask the user if we should replace the book if it already exists instead of always overwritting it
    os.makedirs(library_dir, exist_ok=True)
    output_dir = f"library/{unique_key}"
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    images_dir = os.path.join(output_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    # Extract images
    image_map = {}  # Key: internal_path, Value: local_relative_path
    for item in ebook.get_items():
        if item.get_type() in (ebooklib.ITEM_IMAGE, ebooklib.ITEM_COVER):
            # Normalize filename
            original_fname = os.path.basename(item.get_name())
            # Sanitize filename for OS
            safe_fname = "".join(
                [c for c in original_fname if c.isalpha() or c.isdigit() or c in "._-"]
            ).strip()

            # Save to disk
            local_path = os.path.join(images_dir, safe_fname)
            with open(local_path, "wb") as f:
                f.write(item.get_content())

            # Map keys: We try both the full internal path and just the basename
            # to be robust against messy HTML src attributes
            rel_path = f"images/{safe_fname}"
            image_map[item.get_name()] = rel_path
            image_map[original_fname] = rel_path

    # Generate Table of Contents
    toc_structure = parse_toc_recursive(ebook.toc)
    if not toc_structure:
        toc_structure = get_fallback_toc(ebook)

    spine_chapters = []

    # We iterate over the spine (linear reading order)
    for i, spine_item in enumerate(ebook.spine):
        item_id, linear = spine_item
        item = ebook.get_item_with_id(item_id)

        if not item:
            continue

        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            # Raw content
            raw_content = item.get_content().decode("utf-8", errors="ignore")
            soup = BeautifulSoup(raw_content, "html.parser")

            # A. Fix Images
            for img in soup.find_all(["img", "image"]):
                src = img.get("src") or img.get("xlink:href") or img.get("href")
                if not src:
                    continue

                # Decode URL (part01/image%201.jpg -> part01/image 1.jpg)
                src_decoded = unquote(src)
                filename = os.path.basename(src_decoded)

                # Try to find in map
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

            # B. Clean HTML
            soup = clean_html_content(soup)

            # C. Extract Body Content only
            body = soup.find("body")
            if body:
                # Extract inner HTML of body
                final_html = "".join([str(x) for x in body.contents])
            else:
                final_html = str(soup)

            # D. Create Object
            chapter = ChapterContent(
                id=item_id,
                href=item.get_name(),  # Important: This links TOC to Content
                title=f"Section {i + 1}",  # Fallback, real titles come from TOC
                content=final_html,
                text=extract_plain_text(soup),
                order=i,
            )
            spine_chapters.append(chapter)

    processed_book = Book(
        metadata, spine_chapters, toc_structure, image_map, datetime.now().isoformat()
    )

    # Save to file
    p_path = os.path.join(output_dir, "book.pkl")
    with open(p_path, "wb") as f:
        pickle.dump(processed_book, f)

    return processed_book


if __name__ == "__main__":
    for path in Path("samples").glob("**/*.epub"):
        book = generate_book(path, "library")
