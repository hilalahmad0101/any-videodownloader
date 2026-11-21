"""Video Downloader API (FastAPI + yt-dlp).

Endpoints
    POST /api/info                 -> find the video behind any URL, return metadata + qualities
    POST /api/download             -> start a download job (returns job id)
    GET  /api/progress/{job_id}    -> job status / progress
    GET  /api/downloads            -> files already saved in the download folder
    GET  /api/files/{name}         -> fetch a saved file
    DELETE /api/files/{name}       -> delete a saved file
"""
import re
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Literal
from urllib.parse import urljoin, urlparse

import imageio_ffmpeg
from curl_cffi import requests as cffi_requests
import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import history
import ssflix

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "download" / "reels" / "video 1"
PLAYLIST_ROOT = BASE_DIR / "download" / "reels"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
COOKIE_BROWSERS = {"chrome", "safari", "firefox", "edge", "brave"}
VIDEO_EXTS = (".mp4", ".webm", ".mov", ".m4v", ".mkv", ".m3u8")

app = FastAPI(title="Video Downloader API", version="1.0.0")

jobs: Dict[str, dict] = {}
jobs_lock = threading.Lock()


# ---------- models ----------
class UrlIn(BaseModel):
    url: str
    browser: Optional[str] = None  # optional: reuse this browser's login cookies


class PlaylistDownloadIn(BaseModel):
    urls: List[str]
    folder: str = "playlist"
    quality: str = "1080"
    browser: Optional[str] = None
    audio_language: Literal["auto", "hi", "ur", "en"] = "auto"
    ssflix_server: Optional[int] = Field(default=None, ge=1, le=6)


class DownloadIn(BaseModel):
    url: str
    quality: str = "1080"  # "best", "2160", "1440", "1080", "720", "480", "360", "audio"


# ---------- helpers ----------
def clean_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        raise HTTPException(400, "That doesn't look like a valid URL.")
    return url


def base_opts(browser: Optional[str] = None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ffmpeg_location": FFMPEG,
        "http_headers": {"User-Agent": UA},
        "socket_timeout": 20,
        "retries": 3,
    }
    if browser in COOKIE_BROWSERS:
        opts["cookiesfrombrowser"] = (browser,)
    return opts


def scrape_video_links(url: str) -> List[str]:
    """Fallback: look inside a plain web page for direct video files."""
    try:
        html = cffi_requests.get(url, headers={"User-Agent": UA}, timeout=20).text
    except Exception:  # noqa: BLE001
        return []
    found: List[str] = []
    patterns = [
        r'<meta[^>]+property=["\']og:video(?::url|:secure_url)?["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video',
        r'<source[^>]+src=["\']([^"\']+)',
        r'<video[^>]+src=["\']([^"\']+)',
        r'["\'](https?:[^"\'\s<>]+?\.(?:mp4|webm|mov|m4v|m3u8)(?:\?[^"\'\s<>]*)?)["\']',
    ]
    for p in patterns:
        for m in re.findall(p, html, re.I):
            link = urljoin(url, m.replace("\\/", "/").replace("&amp;", "&"))
            if urlparse(link).path.lower().endswith(VIDEO_EXTS) and link not in found:
                found.append(link)
    return found


def summarize(info: dict) -> dict:
    if info.get("_type") == "playlist" and info.get("entries"):
        info = next((e for e in info["entries"] if e), info)
    heights = sorted(
        {f["height"] for f in info.get("formats", []) if f.get("height") and f.get("vcodec") != "none"},
        reverse=True,
    )
    return {
        "title": info.get("title") or "Untitled video",
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel"),
        "site": info.get("extractor_key"),
        "heights": heights,
        "max_height": heights[0] if heights else None,
    }


def format_selector(quality: str, audio_language: str = "auto") -> str:
    if audio_language != "auto":
        language_filter = ssflix.audio_language_filter(audio_language)
        audio = f"bestaudio{language_filter}"
        muxed = f"best{language_filter}"
        if quality == "audio":
            return f"{audio}/{muxed}"
        height = "" if quality == "best" else f"[height<={int(quality)}]"
        return f"bestvideo{height}+{audio}/{muxed}{height}"
    if quality == "audio":
        return "bestaudio/best"
    if quality == "best":
        return "bestvideo*+bestaudio/best"
    h = int(quality)
    return (
        f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
    )


def clean_err(e: Exception) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")[:400]


def download_item(
    url: str, quality: str, outdir: Path, hook=None, browser: Optional[str] = None, query: Optional[str] = None,
    audio_language: str = "auto", ssflix_server: Optional[int] = None
) -> Path:
    """Download one video into outdir (with page-scrape fallback). Returns the saved file path.
    Every successful download is remembered in history.db (see history.py)."""
    outdir.mkdir(parents=True, exist_ok=True)
    opts = base_opts(browser)
    opts.update(
        {
            "format": format_selector(quality, audio_language),
            "outtmpl": str(outdir / "%(title).150B [%(id)s].%(ext)s"),
            "merge_output_format": "mp4",
            # highest resolution first, then prefer H.264/AAC so files play everywhere (QuickTime, phones)
            "format_sort": ["res", "vcodec:h264", "acodec:aac"],
            "progress_hooks": [hook] if hook else [],
            "restrictfilenames": False,
            "windowsfilenames": True,
        }
    )
    if quality == "audio":
        opts.pop("merge_output_format")
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]

    ssflix_metadata = None
    if ssflix.is_ssflix(url):
        ssflix_metadata = ssflix.selected_entry(url)
        if hook:
            hook({"status": "metadata", "title": ssflix_metadata["title"]})
        if ssflix_server is not None:
            media = ssflix.resolve_media(url, audio_language, server=ssflix_server)
        else:
            media = ssflix.resolve_media(url, audio_language) if audio_language != "auto" else ssflix.resolve_media(url)
        opts["http_headers"].update(media["headers"])
        opts["hls_prefer_native"] = True
        opts["socket_timeout"] = 45
        opts["fragment_retries"] = 5
        opts["skip_unavailable_fragments"] = False
        if audio_language != "auto" and media.get("audio_language") == audio_language:
            # A dub can be a separate labelled stream whose manifest has no LANGUAGE tag.
            opts["format"] = format_selector(quality)
        filename = yt_dlp.utils.sanitize_filename(ssflix_metadata["title"], restricted=False).replace("%", "%%")
        suffix = f" [{ssflix.AUDIO_LANGUAGES[audio_language][0]}]" if audio_language != "auto" else ""
        opts["outtmpl"] = str(outdir / (filename[:180] + suffix + ".%(ext)s"))
        url_target = media["url"]
    else:
        url_target = url

    if audio_language != "auto" and not ssflix_metadata:
        opts["outtmpl"] = str(outdir / ("%(title).150B [%(id)s] [" + ssflix.AUDIO_LANGUAGES[audio_language][0] + "].%(ext)s"))

    def attempt(target: str) -> Path:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=True)
            if info.get("_type") == "playlist" and info.get("entries"):
                info = next(e for e in info["entries"] if e)
            path = Path(info["requested_downloads"][0]["filepath"])
            if ssflix_metadata:
                info.update(ssflix_metadata)
                info["webpage_url"] = url
        try:
            history.record(info, file=str(path), query=query, extra_urls=[url])
        except Exception:  # noqa: BLE001  (history must never break a download)
            pass
        return path

    try:
        return attempt(url_target)
    except yt_dlp.utils.DownloadError:
        if ssflix_metadata:
            raise
        links = scrape_video_links(url)  # fallback: look for a direct video link in the page
        if not links:
            raise
        return attempt(links[0])


def progress_hook(job: dict):
    def hook(d: dict) -> None:
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            job.update(
                status="downloading",
                percent=round(done / total * 100, 1) if total else job.get("percent", 0),
                speed=d.get("speed"),
                eta=d.get("eta"),
            )
        elif d["status"] == "finished":
            job.update(status="processing", percent=100, speed=None, eta=None)

    return hook


def run_job(job_id: str, url: str, quality: str) -> None:
    job = jobs[job_id]
    try:
        job["status"] = "starting"
        path = download_item(url, quality, DOWNLOAD_DIR, progress_hook(job))
        job.update(status="done", percent=100, filename=path.name, size=path.stat().st_size)
    except Exception as e:  # noqa: BLE001
        job.update(status="error", error=clean_err(e))


def safe_folder_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name or "").strip(" .")[:80]
    return name or "playlist"


def playlist_items(urls):
    return [{"url": u, "status": "queued", "percent": 0, "downloaded_bytes": 0,
             "total_bytes": 0, "speed": None, "eta": None} for u in urls]


def run_playlist_job(job_id: str, urls: List[str], quality: str, folder: str, browser: Optional[str],
                     audio_language: str = "auto", ssflix_server: Optional[int] = None) -> None:
    job = jobs[job_id]
    outdir = PLAYLIST_ROOT / folder
    items = job.setdefault("items", playlist_items(urls))
    job.update(status="starting", folder=str(outdir), total=len(urls), done=0, failed=0,
               errors=[], current=None, audio_language=audio_language, ssflix_server=ssflix_server)
    for i, (u, item) in enumerate(zip(urls, items), 1):
        job.update(current=i, percent=0, speed=None, eta=None, status="starting", current_title=u)
        item.update(status="starting")
        components = {}

        def hook(d):
            if d["status"] == "metadata":
                item["title"] = d["title"]
                job["current_title"] = d["title"]
                return
            previous = item["percent"]
            progress_hook(item)(d)
            # Separate audio/video transfers must not make an episode jump backwards.
            item["percent"] = min(99, max(previous, item["percent"]))
            if d["status"] == "downloading":
                key = d.get("filename") or "media"
                components[key] = (d.get("downloaded_bytes") or 0,
                                   d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
                item["downloaded_bytes"] = sum(v[0] for v in components.values())
                item["total_bytes"] = sum(v[1] for v in components.values())
                if not item.get("title") and d.get("info_dict", {}).get("title"):
                    item["title"] = d["info_dict"]["title"]
            job.update(status=item["status"], percent=item["percent"], speed=item.get("speed"),
                       eta=item.get("eta"), current_title=item.get("title") or u)

        try:
            item_dir = outdir
            if ssflix.is_ssflix(u):
                title = ssflix.parse_title(u)
                if title.kind == "serie":
                    item_dir = outdir / f"Season {title.season or 1:02d}"
            download_options = {"audio_language": audio_language}
            if ssflix_server is not None:
                download_options["ssflix_server"] = ssflix_server
            path = download_item(u, quality, item_dir, hook, browser, **download_options)
        except Exception as e:  # noqa: BLE001
            error = clean_err(e)
            item.update(status="error", error=error, speed=None, eta=None)
            job["failed"] += 1
            job["errors"].append({"url": u, "error": error})
        else:
            item.update(status="done", percent=100, filename=path.name, folder=str(item_dir), speed=None, eta=None)
            job["done"] += 1
    job.update(status="done", percent=100, current=None, speed=None, eta=None)


def safe_path(name: str) -> Path:
    p = (DOWNLOAD_DIR / name).resolve()
    if p.parent != DOWNLOAD_DIR.resolve() or not p.is_file():
        raise HTTPException(404, "File not found.")
    return p


# ---------- routes ----------
@app.post("/api/info")
def get_info(body: UrlIn):
    url = clean_url(body.url)
    if ssflix.is_ssflix(url):
        try:
            return {"url": url, **ssflix.selected_entry(url)}
        except ssflix.SSFlixError as e:
            raise HTTPException(422, str(e)) from e
    opts = base_opts(body.browser)
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return {"url": url, **summarize(info)}
    except yt_dlp.utils.DownloadError as first_err:
        links = scrape_video_links(url)
        for link in links:
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(link, download=False)
                return {"url": url, **summarize(info)}
            except yt_dlp.utils.DownloadError:
                continue
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(first_err)).replace("ERROR: ", "")
        raise HTTPException(422, f"No downloadable video found at this URL. {msg[:250]}")


@app.post("/api/download")
def start_download(body: DownloadIn):
    url = clean_url(body.url)
    if body.quality not in {"best", "2160", "1440", "1080", "720", "480", "360", "audio"}:
        raise HTTPException(400, "Invalid quality.")
    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {"id": job_id, "status": "queued", "percent": 0, "url": url}
    threading.Thread(target=run_job, args=(job_id, url, body.quality), daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/playlist/info")
def playlist_info(body: UrlIn):
    """List every video found on a profile / playlist / channel page (no downloading)."""
    url = clean_url(body.url)
    if ssflix.is_ssflix(url):
        try:
            return ssflix.catalog(url)
        except ssflix.SSFlixError as e:
            raise HTTPException(422, str(e)) from e
    opts = base_opts(body.browser)
    opts.update({"noplaylist": False, "extract_flat": "in_playlist", "skip_download": True, "playlistend": 300})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(422, f"Could not read this page. {clean_err(e)[:250]}")
    entries = [e for e in (info.get("entries") or []) if e]
    if not entries:
        raise HTTPException(422, "No list of videos found here. Use the single video page for one video link.")
    videos = []
    for e in entries:
        link = e.get("webpage_url") or e.get("url")
        if not link or not link.startswith("http"):
            continue
        thumbs = e.get("thumbnails") or []
        videos.append(
            {
                "url": link,
                "title": e.get("title") or e.get("id") or "Untitled",
                "duration": e.get("duration"),
                "thumbnail": e.get("thumbnail") or (thumbs[-1].get("url") if thumbs else None),
            }
        )
    return {"title": info.get("title") or info.get("uploader") or urlparse(url).path.strip("/").split("/")[-1] or "playlist", "count": len(videos), "videos": videos}


@app.post("/api/playlist/download")
def playlist_download(body: PlaylistDownloadIn):
    if len(body.urls) > 2000:
        raise HTTPException(400, "Select at most 2000 videos per download batch.")
    urls = list(dict.fromkeys(clean_url(u) for u in body.urls))
    if not urls:
        raise HTTPException(400, "Select at least one video.")
    if body.quality not in {"best", "2160", "1440", "1080", "720", "480", "360", "audio"}:
        raise HTTPException(400, "Invalid quality.")
    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {"id": job_id, "status": "queued", "percent": 0, "kind": "playlist", "items": playlist_items(urls)}
    threading.Thread(
        target=run_playlist_job, args=(job_id, urls, body.quality, safe_folder_name(body.folder), body.browser, body.audio_language, body.ssflix_server), daemon=True
    ).start()
    return {"job_id": job_id}


@app.get("/api/progress/{job_id}")
def progress(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job.")
    return job


@app.get("/api/downloads")
def list_downloads():
    files = [p for p in DOWNLOAD_DIR.iterdir() if p.is_file() and not p.name.startswith(".")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return {
        "folder": str(DOWNLOAD_DIR),
        "files": [{"name": p.name, "size": p.stat().st_size, "modified": p.stat().st_mtime} for p in files],
    }


@app.get("/api/files/{name}")
def get_file(name: str):
    p = safe_path(name)
    return FileResponse(p, filename=p.name)


@app.delete("/api/files/{name}")
def delete_file(name: str):
    safe_path(name).unlink()
    return {"ok": True}


from reels import router as reels_router  # noqa: E402  (Reels Maker page)
from discover import router as discover_router  # noqa: E402  (Discover: search top videos)
from captions import router as captions_router  # noqa: E402  (Facebook titles + hashtags)

app.include_router(reels_router)
app.include_router(discover_router)
app.include_router(captions_router)
app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8765, reload=False)
