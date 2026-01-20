import json
import os
import pickle
from functools import lru_cache
import shutil
from typing import Optional
import uuid

from fastapi import FastAPI, File, Request, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from book import Book, Metadata, ChapterContent, TOCEntry, generate_book
from constants import LIBRARY_PATH

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@lru_cache(maxsize=10)
def load_book_cached(folder_name: str) -> Optional[Book]:
    """
    Loads the book from the pickle file.
    Cached so we don't re-read the disk on every click.
    """
    file_path = os.path.join(LIBRARY_PATH, folder_name, "book.pkl")
    if not os.path.exists(file_path):
        return None

    try:
        with open(file_path, "rb") as f:
            book = pickle.load(f)
        return book
    except Exception as e:
        print(f"Error loading book {folder_name}: {e}")
        return None


def get_progress_path(book_id: str) -> str:
    """Returns the path to the progress.json file for a given book."""
    safe_id = os.path.basename(book_id)
    return os.path.join(LIBRARY_PATH, safe_id, "progress.json")


def save_progress(book_id: str, chapter_index: int):
    """Saves the current chapter index to the book's progress file."""
    try:
        path = get_progress_path(book_id)
        with open(path, "w") as f:
            json.dump({"chapter_index": chapter_index}, f)
    except Exception as e:
        print(f"Error saving progress for {book_id}: {e}")


def load_progress(book_id: str) -> int:
    """Loads the last read chapter index. Defaults to 0 if not found."""
    try:
        path = get_progress_path(book_id)
        if os.path.exists(path):
            with open(path, "r") as f:
                data = json.load(f)
                return data.get("chapter_index", 0)
    except Exception as e:
        print(f"Error loading progress for {book_id}: {e}")
    return 0


@app.get("/", response_class=HTMLResponse)
async def library_view(request: Request):
    """Lists all available processed books."""
    books = []

    if os.path.exists(LIBRARY_PATH):
        for item in os.listdir(LIBRARY_PATH):
            if os.path.isdir(os.path.join(LIBRARY_PATH, item)):
                book = load_book_cached(item)
                if book:
                    books.append(
                        {
                            "id": item,
                            "title": book.metadata.title,
                            "author": ", ".join(book.metadata.authors),
                            "chapters": len(book.spine),
                            "cover_url": f"/read/{item}/images/{book.cover_image}",
                        }
                    )

    return templates.TemplateResponse(
        "library.html", {"request": request, "books": books}
    )


@app.post("/upload")
async def upload_book(files: list[UploadFile] = File(...)):
    """
    Handles EPUB upload.
    Saves to temp, processes with generate_book, clears temp, refreshes library.
    """

    for file in files:
        if not file.filename.endswith(".epub"):
            raise HTTPException(status_code=400, detail="Only .epub files are allowed")

        # Save uploaded file temporarily
        temp_filename = f"temp_{uuid.uuid4()}.epub"
        try:
            with open(temp_filename, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            generate_book(temp_filename, LIBRARY_PATH)
            load_book_cached.cache_clear()

        except Exception as e:
            print(f"Error processing book: {e}")
            raise HTTPException(status_code=500, detail="Failed to process book")
        finally:
            # Cleanup temp file
            if os.path.exists(temp_filename):
                os.remove(temp_filename)

    return RedirectResponse(url="/", status_code=303)


@app.post("/delete/{book_id}")
async def delete_book(book_id: str):
    """
    Deletes a book folder and refreshes the cache.
    """
    # Security: Sanitizing to ensure no one deletes ../system_files
    safe_id = os.path.basename(book_id)
    book_path = os.path.join(LIBRARY_PATH, safe_id)

    if os.path.exists(book_path):
        try:
            shutil.rmtree(book_path)
            load_book_cached.cache_clear()
        except Exception as e:
            print(f"Error deleting book {safe_id}: {e}")
            raise HTTPException(status_code=500, detail="Failed to delete book")
    else:
        raise HTTPException(status_code=404, detail="Book not found")

    return RedirectResponse(url="/", status_code=303)


@app.get("/read/{book_id}", response_class=HTMLResponse)
async def redirect_to_last_read(request: Request, book_id: str):
    """Helper to go to the last read chapter."""
    last_chapter_index = load_progress(book_id)
    return RedirectResponse(url=f"/read/{book_id}/{last_chapter_index}")


@app.get("/read/{book_id}/{chapter_index}", response_class=HTMLResponse)
async def read_chapter(request: Request, book_id: str, chapter_index: int):
    """The main reader interface."""
    book = load_book_cached(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    if chapter_index < 0 or chapter_index >= len(book.spine):
        raise HTTPException(status_code=404, detail="Chapter not found")

    save_progress(book_id, chapter_index)
    current_chapter = book.spine[chapter_index]

    # Calculate Prev/Next links
    prev_idx = chapter_index - 1 if chapter_index > 0 else None
    next_idx = chapter_index + 1 if chapter_index < len(book.spine) - 1 else None

    return templates.TemplateResponse(
        "reader.html",
        {
            "request": request,
            "book": book,
            "current_chapter": current_chapter,
            "chapter_index": chapter_index,
            "book_id": book_id,
            "prev_idx": prev_idx,
            "next_idx": next_idx,
        },
    )


@app.get("/read/{book_id}/images/{image_name}")
async def serve_image(book_id: str, image_name: str):
    """
    Serves images specifically for a book.
    The HTML contains <img src="images/pic.jpg">.
    The browser resolves this to /read/{book_id}/images/pic.jpg.
    """
    # Security check: ensure book_id is clean
    safe_book_id = os.path.basename(book_id)
    safe_image_name = os.path.basename(image_name)

    img_path = os.path.join(LIBRARY_PATH, safe_book_id, "images", safe_image_name)

    if not os.path.exists(img_path):
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(img_path)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8123)
