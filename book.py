# This file is heavily inspired by https://github.com/karpathy/reader3/blob/master/reader3.py

from dataclasses import dataclass
from ebooklib import epub
from pathlib import Path


@dataclass
class Metadata:
    title: str
    authors: list[str]


@dataclass
class Book:
    metadata: Metadata


def _process_metadata(ebook):
    title = ebook.get_metadata("DC", "title")
    title = title[0][0] if title else "Untitled"
    authors = ebook.get_metadata("DC", "creator")
    authors = [author[0] for author in authors] if authors else []
    return Metadata(title, authors)


def generate_book(path) -> Book:
    ebook = epub.read_epub(path)
    metadata = _process_metadata(ebook)
    return Book(metadata)


if __name__ == "__main__":
    for path in Path("samples").glob("**/*.epub"):
        book = generate_book(path)
        print(book)
