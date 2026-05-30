"""
Web scraper for Riphah International University website.

Fetches key public pages from riphah.edu.pk, extracts clean text,
chunks it, embeds with nomic-embed-text, and stores in the
`university_knowledge` Qdrant collection.

Triggered automatically on startup when the collection is empty,
and can be triggered manually via POST /api/scrape.
"""

import re
import time
import hashlib
import threading

import httpx
from qdrant_client.models import Distance, VectorParams, PointStruct

from backend.models.openai_client import embed
from backend.rag.embeddings import get_client

COLLECTION_NAME = "university_knowledge"

# Seed pages to scrape — publicly accessible Riphah pages
RIPHAH_SEED_URLS = [
    "https://riphah.edu.pk/",
    "https://riphah.edu.pk/admissions/",
    "https://riphah.edu.pk/programs/",
    "https://riphah.edu.pk/faculties/",
    "https://riphah.edu.pk/campuses/",
    "https://riphah.edu.pk/about-riphah/",
    "https://riphah.edu.pk/scholarships/",
    "https://riphah.edu.pk/international-students/",
    "https://riphah.edu.pk/research/",
    "https://riphah.edu.pk/student-life/",
]

_scraping_lock = threading.Lock()
_scraping_active = False


def _clean_html(html: str) -> str:
    """Remove HTML tags and return clean text."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        for tag in soup.find_all(["script", "style", "nav", "footer", "header", "iframe", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
    except Exception:
        # Fallback: strip tags with regex
        text = re.sub(r"<[^>]+>", " ", html)

    lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 20]
    return "\n".join(lines)


def _chunk(text: str, size: int = 400, overlap: int = 40) -> list[str]:
    words = text.split()
    chunks = []
    for i in range(0, len(words), size - overlap):
        c = " ".join(words[i : i + size])
        if c.strip():
            chunks.append(c)
    return chunks


def _ensure_collection() -> None:
    client = get_client()
    existing = [c.name for c in client.get_collections().collections]
    if COLLECTION_NAME not in existing:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=768, distance=Distance.COSINE),
        )


def _url_base_id(url: str) -> int:
    """Stable integer ID base derived from URL hash."""
    return int(hashlib.md5(url.encode()).hexdigest()[:8], 16)


def scrape_and_ingest(urls: list[str] | None = None) -> int:
    """
    Scrape pages, embed chunks, upsert into Qdrant.
    Returns total chunks ingested.
    Blocks while running; use trigger_background_scrape for async.
    """
    global _scraping_active

    with _scraping_lock:
        if _scraping_active:
            print("[Scraper] Already running, skipping.")
            return 0
        _scraping_active = True

    total = 0
    try:
        target = urls or RIPHAH_SEED_URLS
        _ensure_collection()
        client = get_client()

        headers = {
            "User-Agent": "AskRiphah/1.0 (+https://riphah.edu.pk; university AI assistant)"
        }
        with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as http:
            for url in target:
                try:
                    print(f"[Scraper] Fetching {url} ...")
                    resp = http.get(url)
                    if resp.status_code != 200:
                        print(f"[Scraper] {url} → HTTP {resp.status_code}, skipping")
                        continue

                    text = _clean_html(resp.text)
                    if len(text) < 150:
                        print(f"[Scraper] {url} → too little text ({len(text)} chars), skipping")
                        continue

                    chunks = _chunk(text)
                    base_id = _url_base_id(url)
                    points = []

                    for i, chunk in enumerate(chunks):
                        vec = embed(chunk)
                        if not vec:
                            continue
                        points.append(
                            PointStruct(
                                id=base_id + i,
                                vector=vec,
                                payload={
                                    "text": chunk,
                                    "source_url": url,
                                    "filename": f"web:{url}",
                                    "source": "web_scrape",
                                    "scraped_at": time.time(),
                                },
                            )
                        )

                    if points:
                        client.upsert(collection_name=COLLECTION_NAME, points=points)
                        total += len(points)
                        print(f"[Scraper] {url} → {len(points)} chunks stored")

                    time.sleep(1.5)  # Polite crawl rate

                except Exception as e:
                    print(f"[Scraper] Error on {url}: {e}")
                    continue

    finally:
        with _scraping_lock:
            _scraping_active = False

    print(f"[Scraper] Done. Total chunks ingested: {total}")
    return total


def trigger_background_scrape(urls: list[str] | None = None) -> None:
    """Start scraping in a daemon thread — returns immediately."""
    t = threading.Thread(target=scrape_and_ingest, args=(urls,), daemon=True, name="riphah-scraper")
    t.start()


def is_collection_populated() -> bool:
    """Returns True if university_knowledge collection has any content."""
    try:
        client = get_client()
        existing = [c.name for c in client.get_collections().collections]
        if COLLECTION_NAME not in existing:
            return False
        info = client.get_collection(COLLECTION_NAME)
        return (info.points_count or 0) > 0
    except Exception:
        return False


def is_scraping() -> bool:
    return _scraping_active
