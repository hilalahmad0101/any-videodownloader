"""Download history in SQLite: once a video is downloaded it is remembered forever,
even if the file is deleted later, so Discover never suggests or downloads it again.

Each video is stored once (key = "<site>:<id>") with several aliases (its URLs, old ids)
so it is recognised whether it is seen in search results or pasted as a link.
"""
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, List, Optional

DB_PATH = Path(__file__).resolve().parent / "history.db"
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with _lock, _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS downloads (
                key           TEXT PRIMARY KEY,
                site          TEXT,
                video_id      TEXT,
                url           TEXT,
                title         TEXT,
                uploader      TEXT,
                views         INTEGER,
                likes         INTEGER,
                query         TEXT,
                file          TEXT,
                downloaded_at REAL
            );
            CREATE TABLE IF NOT EXISTS aliases (
                alias TEXT PRIMARY KEY,
                key   TEXT NOT NULL REFERENCES downloads(key) ON DELETE CASCADE
            );
            """
        )


def norm_url(url: Optional[str]) -> Optional[str]:
    """Same video, different spellings -> one string (no scheme, www/m., query noise, trailing slash)."""
    if not url:
        return None
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^(www\.|m\.)", "", u)
    if u.startswith("youtube.com/watch") or u.startswith("youtu.be/"):
        m = re.search(r"(?:v=|youtu\.be/)([\w-]{11})", u)
        if m:
            return "youtube.com/watch?v=" + m.group(1)
    u = u.split("#")[0].split("?")[0].rstrip("/")
    return "url:" + u


def video_key(site: Optional[str], vid: Optional[str]) -> Optional[str]:
    if not site or not vid:
        return None
    return f"{site.lower()}:{vid}"


def aliases_for(info: dict, extra_urls: Iterable[str] = ()) -> List[str]:
    """Every string that identifies this video (works for full info and flat search entries)."""
    site = info.get("extractor_key") or info.get("ie_key") or info.get("extractor")
    out = [
        video_key(site, info.get("id")),
        norm_url(info.get("webpage_url")),
        norm_url(info.get("original_url")),
        norm_url(info.get("url")),
    ]
    out += [norm_url(u) for u in extra_urls]
    for old in info.get("_old_archive_ids") or []:  # e.g. "bilibili 1173..." (av id)
        parts = old.split(" ", 1)
        if len(parts) == 2:
            out.append(video_key(parts[0], parts[1]))
    return [a for a in dict.fromkeys(out) if a]


def record(info: dict, file: Optional[str] = None, query: Optional[str] = None, extra_urls: Iterable[str] = ()) -> None:
    als = aliases_for(info, extra_urls)
    if not als:
        return
    site = info.get("extractor_key") or info.get("extractor") or "web"
    key = video_key(site, info.get("id")) or als[0]
    with _lock, _conn() as c:
        c.execute(
            """INSERT INTO downloads (key, site, video_id, url, title, uploader, views, likes, query, file, downloaded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET file=excluded.file, downloaded_at=excluded.downloaded_at,
                   query=COALESCE(excluded.query, downloads.query)""",
            (
                key, site, info.get("id"), info.get("webpage_url") or info.get("original_url"),
                info.get("title"), info.get("uploader") or info.get("channel"),
                info.get("view_count"), info.get("like_count"), query, file, time.time(),
            ),
        )
        c.executemany("INSERT OR IGNORE INTO aliases (alias, key) VALUES (?, ?)", [(a, key) for a in als + [key]])


def is_downloaded(info: dict, extra_urls: Iterable[str] = ()) -> bool:
    als = aliases_for(info, extra_urls)
    if not als:
        return False
    with _conn() as c:
        q = f"SELECT 1 FROM aliases WHERE alias IN ({','.join('?' * len(als))}) LIMIT 1"
        return c.execute(q, als).fetchone() is not None


def list_all(limit: int = 500) -> List[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM downloads ORDER BY downloaded_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def forget(key: str) -> bool:
    """Remove a video from history so it can be suggested / downloaded again."""
    with _lock, _conn() as c:
        c.execute("DELETE FROM aliases WHERE key = ?", (key,))
        return c.execute("DELETE FROM downloads WHERE key = ?", (key,)).rowcount > 0


init()
