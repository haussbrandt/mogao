from dataclasses import dataclass
from ebooklib import epub
from pathlib import Path


@dataclass
class Metadata:
    title: str
    authors: list[str]


class Book:
    def __init__(self, path) -> None:
        self.ebook = epub.read_epub(path)
        self.metadata = self._process_metadata()

    def _process_metadata(self):
        title = self.ebook.get_metadata("DC", "title")
        title = title[0][0] if title else "Untitled"
        authors = self.ebook.get_metadata("DC", "creator")
        authors = [author[0] for author in authors] if authors else []
        return Metadata(title, authors)


if __name__ == "__main__":
    for book in Path("samples").glob("**/*.epub"):
        book = Book(book)
        print(book.metadata)
