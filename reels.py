"""Reels Maker: cut a long video into short reels (separate from the normal downloader).

Endpoints
    POST /api/reels/start           -> start a job (from a URL or an already-downloaded file)
    GET  /api/reels/progress/{id}   -> job status / progress
    GET  /api/reels/projects        -> every folder that has reels in it
    GET  /api/reels/file/{folder}/{name} -> fetch one reel
    DELETE /api/reels/project/{folder}   -> delete a project folder

Output layout
    download/<video name>/<video name>.mp4     (source video)
    download/<video name>/reels/reel 01.mp4 ...
"""
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import imageio_ffmpeg

BASE_DIR = Path(__file__).resolve().parent
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
REELS_ROOT = BASE_DIR / "download"
TMP_DIR = REELS_ROOT / ".reels_tmp"
REELS_SUBDIR = "reels"
QUALITIES = {"best", "2160", "1440", "1080", "720", "480", "360"}
FORMATS = {"vertical_blur", "vertical_crop", "original"}

router = APIRouter(prefix="/api/reels")
jobs: Dict[str, dict] = {}


class ReelsIn(BaseModel):
    url: Optional[str] = None  # a link to download first
    file: Optional[str] = None  # or: a file already in the downloads folder
    folder: Optional[str] = None  # custom project name (default: video title)
    clip_seconds: int = 180  # 60..300
    count: int = 10  # 1..30
    layout: str = "spread"  # "spread" across the whole video, or "sequential" from the start
    format: str = "vertical_blur"
    quality: str = "1080"
    browser: Optional[str] = None


# ---------- helpers ----------
def core():
    """Shared downloader helpers from main.py (imported lazily: main.py imports this module)."""
    import main

    return main


def safe_folder_name(name: str) -> str:
    return core().safe_folder_name(name)


def video_duration(path: Path) -> float:
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if not m:
        raise RuntimeError("Could not read the video length.")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def plan_clips(duration: float, clip: float, count: int, layout: str):
    """Return [(start, length)] — never overlapping, never past the end."""
    if duration <= clip:
        return [(0.0, duration)]
    count = max(1, min(count, int(duration // clip)))  # can't fit more than this without overlap
    if layout == "sequential" or count == 1:
        return [(i * clip, clip) for i in range(count)]
    step = (duration - clip) / (count - 1)  # evenly spread, first starts at 0, last ends at the end
    return [(round(i * step, 2), clip) for i in range(count)]


def video_filter(fmt: str) -> Optional[str]:
    if fmt == "vertical_crop":
        return "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1"
    if fmt == "vertical_blur":
        return (
            "split[a][b];"
            "[a]scale=540:960:force_original_aspect_ratio=increase,crop=540:960,boxblur=20:2,scale=1080:1920[bg];"
            "[b]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
    return None


def cut_clip(src: Path, dst: Path, start: float, length: float, fmt: str, on_progress) -> None:
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.2f}", "-i", str(src), "-t", f"{length:.2f}"]
    vf = video_filter(fmt)
    if vf:
        cmd += ["-filter_complex", vf]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats", str(dst),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc.stdout:
        if line.startswith("out_time_us=") and line.strip()[12:].isdigit():
            on_progress(min(1.0, int(line.strip()[12:]) / 1e6 / length))
    err = proc.stderr.read()
    if proc.wait() != 0:
        dst.unlink(missing_ok=True)
        raise RuntimeError(err.strip()[-300:] or "ffmpeg failed")


def strip_id(stem: str) -> str:
    return re.sub(r"\s*\[[^\]]+\]$", "", stem)  # "My video [abc123]" -> "My video"


def project_name(raw: str) -> str:
    name = safe_folder_name(raw)
    return f"{name} (reels)" if name.lower() in {"reels", ".reels_tmp"} else name  # don't mix with other folders


def project_dir(folder: str) -> Path:
    p = (REELS_ROOT / folder).resolve()
    if p.parent != REELS_ROOT.resolve():
        raise HTTPException(404, "Folder not found.")
    return p


def list_reels(folder: Path):
    d = folder / REELS_SUBDIR
    if not d.is_dir():
        return []
    files = sorted(p for p in d.iterdir() if p.suffix == ".mp4" and not p.name.startswith("."))
    return [{"name": p.name, "size": p.stat().st_size} for p in files]


# ---------- job ----------
def run_reels_job(job_id: str, body: ReelsIn) -> None:
    job = jobs[job_id]
    try:
        # 1) get the source video
        if body.url:
            job.update(status="downloading", step="Downloading video")
            tmp = TMP_DIR / job_id
            m = core()
            src = m.download_item(m.clean_url(body.url), body.quality, tmp, m.progress_hook(job), body.browser)
            name = project_name(body.folder or strip_id(src.stem))
            outdir = REELS_ROOT / name
            outdir.mkdir(parents=True, exist_ok=True)
            final = outdir / f"{name}{src.suffix}"
            shutil.move(str(src), final)
            shutil.rmtree(tmp, ignore_errors=True)
            src = final
        else:
            downloads = core().DOWNLOAD_DIR.resolve()
            src = (downloads / body.file).resolve()
            if src.parent != downloads or not src.is_file():
                raise RuntimeError("That file is not in the downloads folder.")
            name = project_name(body.folder or strip_id(src.stem))
            outdir = REELS_ROOT / name
            outdir.mkdir(parents=True, exist_ok=True)

        # 2) plan the cuts
        job.update(status="processing", step="Reading video", percent=0, speed=None, eta=None)
        duration = video_duration(src)
        clips = plan_clips(duration, body.clip_seconds, body.count, body.layout)
        reels_dir = outdir / REELS_SUBDIR
        reels_dir.mkdir(exist_ok=True)
        job.update(folder=name, path=str(reels_dir), duration=duration, total=len(clips), done=0, files=[])

        # 3) cut each reel
        for i, (start, length) in enumerate(clips, 1):
            job.update(current=i, step=f"Making reel {i} of {len(clips)}")
            dst = reels_dir / f"reel {i:02d}.mp4"

            def on_progress(frac, i=i):
                job["percent"] = round(((i - 1) + frac) / len(clips) * 100, 1)

            cut_clip(src, dst, start, length, body.format, on_progress)
            job["done"] = i
            job["files"].append({"name": dst.name, "start": start, "length": length, "size": dst.stat().st_size})
        job.update(status="done", percent=100, step="Done", current=None)
    except Exception as e:  # noqa: BLE001
        job.update(status="error", error=core().clean_err(e))


# ---------- routes ----------
@router.post("/start")
def start(body: ReelsIn):
    if not body.url and not body.file:
        raise HTTPException(400, "Give a video link or pick a downloaded file.")
    if not 60 <= body.clip_seconds <= 300:
        raise HTTPException(400, "Reel length must be between 1 and 5 minutes.")
    if not 1 <= body.count <= 30:
        raise HTTPException(400, "Number of reels must be between 1 and 30.")
    if body.layout not in {"spread", "sequential"} or body.format not in FORMATS or body.quality not in QUALITIES:
        raise HTTPException(400, "Invalid option.")
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"id": job_id, "status": "queued", "percent": 0, "step": "Queued"}
    threading.Thread(target=run_reels_job, args=(job_id, body), daemon=True).start()
    return {"job_id": job_id}


@router.get("/progress/{job_id}")
def progress(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job.")
    return job


@router.get("/projects")
def projects():
    out = []
    if REELS_ROOT.is_dir():
        for d in REELS_ROOT.iterdir():
            reels = list_reels(d) if d.is_dir() else []
            if reels:
                out.append({"folder": d.name, "path": str(d / REELS_SUBDIR), "reels": reels, "modified": d.stat().st_mtime})
    out.sort(key=lambda p: p["modified"], reverse=True)
    return {"projects": out}


@router.get("/file/{folder}/{name}")
def get_reel(folder: str, name: str):
    d = project_dir(folder) / REELS_SUBDIR
    p = (d / name).resolve()
    if p.parent != d or not p.is_file():
        raise HTTPException(404, "File not found.")
    return FileResponse(p, filename=f"{folder} - {p.name}")


@router.delete("/project/{folder}")
def delete_project(folder: str):
    d = project_dir(folder)
    if not (d / REELS_SUBDIR).is_dir():
        raise HTTPException(404, "Not a reels folder.")
    shutil.rmtree(d)
    return {"ok": True}
