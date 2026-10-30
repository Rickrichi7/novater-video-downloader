"""NOVATER Video Downloader backend — standalone engine.

No Cobalt, no fbdown, no third-party download service. The server itself
fetches the video with yt-dlp and streams the file straight back to the
browser, so one tap on Download = the file saves on the user's device.

Endpoints:
  GET  /                 health check
  POST /api/download     {"url": ..., "quality": "best"|"720p"|"480p"|"audio"}
                         -> the video/audio file as a download
"""
import os
import re
import shutil
import tempfile
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

import yt_dlp

app = FastAPI(title="NOVATER Video Downloader")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

URL_RE = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)

QUALITY_FORMATS = {
    "best": "bestvideo+bestaudio/best",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
}


class DownloadRequest(BaseModel):
    url: str
    quality: str = "best"


@app.get("/")
def health():
    return {"status": "ok", "engine": "yt-dlp", "service": "novater-video-downloader"}


@app.post("/api/download")
def download(req: DownloadRequest):
    url = (req.url or "").strip()
    if not URL_RE.match(url):
        raise HTTPException(status_code=400, detail="That does not look like a valid link.")
    quality = req.quality if req.quality in QUALITY_FORMATS or req.quality == "audio" else "best"

    workdir = tempfile.mkdtemp(prefix="novater-dl-")
    try:
        ydl_opts = {
            "outtmpl": os.path.join(workdir, "%(title).80s-%(id)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 60,
            "retries": 3,
            "extractor_retries": 3,
            "fragment_retries": 3,
            "merge_output_format": "mp4",
        }
        if quality == "audio":
            ydl_opts["format"] = "bestaudio/best"
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        else:
            ydl_opts["format"] = QUALITY_FORMATS[quality]

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        # A post can resolve to more than one clip (e.g. a tweet with a
        # quoted video). Take the first media file produced: that is the
        # post's own video. Never trust prepare_filename for this.
        media_exts = (".mp4", ".webm", ".mkv", ".mov", ".mp3", ".m4a", ".opus")
        produced = [os.path.join(workdir, f) for f in os.listdir(workdir)
                    if os.path.isfile(os.path.join(workdir, f))
                    and f.lower().endswith(media_exts)]
        if not produced:
            raise HTTPException(status_code=502, detail="Download finished but no file was produced.")
        if quality == "audio":
            mp3s = [f for f in produced if f.endswith(".mp3")]
            filename = mp3s[0] if mp3s else produced[0]
        else:
            produced.sort(key=os.path.getmtime)
            filename = produced[0]

        download_name = os.path.basename(filename)
        media_type = "audio/mpeg" if filename.endswith(".mp3") else "video/mp4"
        return FileResponse(
            filename,
            media_type=media_type,
            filename=download_name,
            background=BackgroundTask(shutil.rmtree, workdir, True),
        )
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except Exception as exc:  # yt-dlp raises DownloadError and friends
        shutil.rmtree(workdir, ignore_errors=True)
        msg = str(exc)
        if "Unsupported URL" in msg or "No video formats" in msg:
            raise HTTPException(status_code=422, detail="This link is not supported or has no downloadable video.")
        if "Private" in msg or "login" in msg.lower():
            raise HTTPException(status_code=422, detail="This video is private or needs a login, so it cannot be downloaded.")
        raise HTTPException(status_code=502, detail="Could not download that video. Try another public link.")
