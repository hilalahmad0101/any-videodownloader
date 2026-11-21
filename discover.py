"""Discover: type a topic (e.g. "funny chinese video"), get the most watched + most liked videos
you have NOT downloaded before, and download them with one click.

Sources: YouTube and Bilibili (China's big video site) via yt-dlp search.
TikTok / Douyin have no public search that yt-dlp supports, so they are not searched here
(single TikTok links still download fine on the normal page).

Endpoints
    POST   /api/discover/search          -> top N fresh videos for a topic
    POST   /api/discover/download        -> download chosen videos (job; poll /api/progress/{id})
    GET    /api/discover/history         -> everything ever downloaded
    DELETE /api/discover/history/{key}   -> forget one video (it can be suggested again)
"""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import yt_dlp
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import history

router = APIRouter(prefix="/api/discover")

SOURCES = {"all", "youtube", "bilibili"}
LIKE_WEIGHT = 20  # one like counts as much as 20 views when ranking
SHORT_MAX = 180  # "short clips only" = 3 minutes or less


class SearchIn(BaseModel):
    query: str
    source: str = "all"
    count: int = 3
    short_only: bool = False
    browser: Optional[str] = None  # reuse this browser's login cookies (fewer Bilibili blocks)


class DiscoverDownloadIn(BaseModel):
    urls: List[str]
    query: Optional[str] = None
    quality: str = "1080"
    captions: Dict[str, str] = {}  # url -> Facebook title + hashtags, saved next to the video


def core():
    import main  # lazy: main.py imports this module

    return main


def _opts(browser: Optional[str], **extra) -> dict:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 20, **extra}
    if browser in core().COOKIE_BROWSERS:
        opts["cookiesfrombrowser"] = (browser,)
    return opts


SEARCH_CACHE_SECONDS = 15 * 60
_search_cache: Dict[str, tuple] = {}  # term -> (time, results): repeat searches don't hit the site again


def _flat_search(term: str, browser: Optional[str] = None, tries: int = 5) -> Optional[List[dict]]:
    """Search results without details. None = the site refused (Bilibili often answers 412 to bursts)."""
    hit = _search_cache.get(term)
    if hit and time.time() - hit[0] < SEARCH_CACHE_SECONDS:
        return hit[1]
    for attempt in range(tries):
        try:
            with yt_dlp.YoutubeDL(_opts(browser, extract_flat="in_playlist")) as ydl:
                info = ydl.extract_info(term, download=False)
            found = [e for e in (info.get("entries") or []) if e and e.get("url")]
            _search_cache[term] = (time.time(), found)
            return found
        except yt_dlp.utils.DownloadError:
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1))  # 2s, 4s, 6s, 8s
    return None


_info_cache: Dict[str, tuple] = {}  # url -> (time, info): repeat searches are near-instant


def _full_info(url: str, browser: Optional[str] = None) -> Optional[dict]:
    hit = _info_cache.get(url)
    if hit and time.time() - hit[0] < SEARCH_CACHE_SECONDS:
        return hit[1]
    try:
        with yt_dlp.YoutubeDL(_opts(browser, noplaylist=True)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:  # noqa: BLE001  (private / removed / region-locked: just skip it)
        return None
    keep = ("id", "extractor_key", "_type", "webpage_url", "original_url", "_old_archive_ids", "title", "thumbnail",
            "duration", "uploader", "channel", "view_count", "like_count", "tags", "description")
    info = {k: info.get(k) for k in keep}  # small copy: full info holds every format and is large
    _info_cache[url] = (time.time(), info)
    return info


def _score(v: dict) -> int:
    return (v.get("view_count") or 0) + LIKE_WEIGHT * (v.get("like_count") or 0)


def _https(u: Optional[str]) -> Optional[str]:
    return "https://" + u[7:] if u and u.startswith("http://") else u


@router.post("/search")
def search(body: SearchIn):
    query = body.query.strip()
    if not query:
        raise HTTPException(400, "Type what kind of videos you want.")
    if body.source not in SOURCES:
        raise HTTPException(400, "Unknown source.")
    count = max(1, min(body.count, 10))

    # 1) quick search: lots of candidates, drop anything already downloaded
    candidates: List[dict] = []
    skipped = 0
    notes: List[str] = []
    if body.source in {"all", "youtube"}:
        yt = []
        found = _flat_search(f"ytsearch40:{query}", body.browser)
        if found is None:
            notes.append("YouTube search failed, try again.")
        for e in found or []:
            if history.is_downloaded(e):
                skipped += 1
                continue
            if body.short_only and e.get("duration") and e["duration"] > SHORT_MAX:
                continue
            if e.get("live_status") in {"is_live", "is_upcoming"}:
                continue
            yt.append(e)
        yt.sort(key=lambda e: e.get("view_count") or 0, reverse=True)  # YouTube search already gives views
        candidates += yt[: count * 3]
    if body.source in {"all", "bilibili"}:
        bili = []
        found = _flat_search(f"bilisearch30:{query}", body.browser)
        if found is None:
            notes.append(
                "Bilibili blocked the search (anti-bot)."
                + ("" if body.browser else " Tip: choose your browser under “Use my browser's login” after opening bilibili.com in it once.")
                + (" Showing YouTube only." if body.source == "all" else " Try again in a minute.")
            )
        for e in found or []:
            if history.is_downloaded(e):
                skipped += 1
                continue
            bili.append(e)
        candidates += bili[: count * (5 if body.short_only else 3)]  # Bilibili search gives no stats yet, so check more of them

    if not candidates:
        if notes:
            raise HTTPException(503, " ".join(notes))
        raise HTTPException(404, "No new videos found for that search. Try other words (Chinese words work best for Bilibili).")

    # 2) full details (real views + likes) for the candidates, in parallel.
    #    Bilibili gets only 3 at a time: bigger bursts are what trigger its 412 blocks.
    is_bili = lambda e: "bilibili" in e["url"]  # noqa: E731
    with ThreadPoolExecutor(max_workers=8) as yt_pool, ThreadPoolExecutor(max_workers=3) as bili_pool:
        futures = [((bili_pool if is_bili(e) else yt_pool).submit(_full_info, e["url"], body.browser), e) for e in candidates]
        infos = [(e, f.result()) for f, e in futures]

    results, seen = [], set()
    for flat, info in infos:
        if not info or info.get("_type") == "playlist":
            continue
        key = history.video_key(info.get("extractor_key"), info.get("id"))
        if key in seen or history.is_downloaded(info, [flat["url"]]):
            continue
        if body.short_only and (info.get("duration") or 0) > SHORT_MAX:
            continue
        seen.add(key)
        results.append(
            {
                "key": key,
                "url": info.get("webpage_url") or flat["url"],
                "title": info.get("title") or "Untitled",
                "thumbnail": _https(info.get("thumbnail")),
                "duration": info.get("duration"),
                "uploader": info.get("uploader") or info.get("channel"),
                "site": info.get("extractor_key"),
                "views": info.get("view_count"),
                "likes": info.get("like_count"),
                "score": _score(info),
                "tags": [t for t in (info.get("tags") or []) if isinstance(t, str)][:20],
                "description": (info.get("description") or "")[:600],
            }
        )
    results.sort(key=lambda v: v["score"], reverse=True)
    if not results:
        raise HTTPException(404, "Every video found was already downloaded or unavailable. Try different words.")
    return {"query": query, "videos": results[:count], "skipped_downloaded": skipped, "notes": notes}


def _run(job_id: str, urls: List[str], query: Optional[str], quality: str, captions: Dict[str, str]) -> None:
    main = core()
    job = main.jobs[job_id]
    folder = main.safe_folder_name(query or "discover")
    outdir = main.BASE_DIR / "download" / "discover" / folder
    job.update(status="downloading", folder=str(outdir), total=len(urls), done=0, failed=0, skipped=0,
               errors=[], current=None, files=[])
    for i, u in enumerate(urls, 1):
        job.update(current=i, percent=0)
        if history.is_downloaded({}, [u]):  # double click / another tab got it first
            job["skipped"] += 1
            continue
        try:
            try:
                path = main.download_item(u, quality, outdir, main.progress_hook(job), query=query)
            except Exception:  # noqa: BLE001  (CDNs like Bilibili's throw short-lived 503s: one more go)
                time.sleep(3)
                path = main.download_item(u, quality, outdir, main.progress_hook(job), query=query)
        except Exception as e:  # noqa: BLE001
            job["failed"] += 1
            job["errors"].append({"url": u, "error": main.clean_err(e)})
        else:
            job["done"] += 1
            job["files"].append(path.name)
            if captions.get(u):  # ready-to-paste Facebook title + hashtags
                path.with_name(path.stem + ".facebook.txt").write_text(captions[u], encoding="utf-8")
    job.update(status="done", percent=100, current=None)


@router.post("/download")
def download(body: DiscoverDownloadIn):
    main = core()
    urls = [main.clean_url(u) for u in body.urls][:20]
    if not urls:
        raise HTTPException(400, "Pick at least one video.")
    if body.quality not in {"best", "2160", "1440", "1080", "720", "480", "360", "audio"}:
        raise HTTPException(400, "Invalid quality.")
    job_id = uuid.uuid4().hex[:12]
    with main.jobs_lock:
        main.jobs[job_id] = {"id": job_id, "status": "queued", "percent": 0, "kind": "discover"}
    threading.Thread(target=_run, args=(job_id, urls, body.query, body.quality, body.captions), daemon=True).start()
    return {"job_id": job_id}


@router.get("/history")
def get_history():
    return {"videos": history.list_all()}


@router.delete("/history/{key:path}")
def forget(key: str):
    if not history.forget(key):
        raise HTTPException(404, "Not in history.")
    return {"ok": True}
