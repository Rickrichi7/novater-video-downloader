"""NOVATER Video Downloader backend — standalone engine.

No Cobalt, no fbdown, no third-party download service. The server itself
fetches the video with yt-dlp and streams the file straight back to the
browser, so one tap on Download = the file saves on the user's device.

Endpoints:
  GET  /                 health check
  POST /api/download     {"url": ..., "quality": "best"|"720p"|"480p"|"audio"}
                         -> the video/audio file as a download

User accounts (IPTV cloud save/restore, added 2026-11-07):
  POST /api/user/register        {"username", "password"?} -> 201 | 409 taken
  POST /api/user/login           {"username", "password"} -> 200 | 401
  POST /api/user/save            {"username", "password", "data"} -> 200 | 401/404/409/413
  POST /api/user/load            {"username", "password"} -> 200 | 401/404
  POST /api/user/change-password {"username", "oldPassword", "newPassword"} -> 200 | 401
  GET  /api/user/health          {"status", "store"}

TV share links (QR to Muse, added 2026-11-07):
  POST /api/share                {"text": "..."} -> {"code": "<short code>"}
  GET  /s/<code>                 plain readable page with the shared text

Storage: a PRIVATE GitHub repo, one JSON file per user
(users/<username>.json), via the GitHub Contents API. Render's disk is
ephemeral, so nothing is ever kept on the server's filesystem in
production. The GitHub token lives ONLY in env vars (GITHUB_USER_TOKEN /
GITHUB_USER_REPO) — never in code, logs, or the APK. Local-file mode
(IPTV_USER_STORE=local) exists ONLY for local testing; the deployed
backend must never run in it.
"""
import base64
import hashlib
import hmac
import html
import json
import os
import random
import re
import shutil
import string
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
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


# =====================================================================
# IPTV user accounts — cloud save / restore
# =====================================================================

DEFAULT_PASSWORD = "5555"      # Ron's order: default for new accounts.
DEFAULT_ADULT_PIN = "1212"     # Ron's order: default adult code for all.
MAX_PAYLOAD_BYTES = 2_000_000  # 2 MB — far above any realistic user list.
PBKDF2_ITERATIONS = 600_000    # OWASP recommendation for PBKDF2-HMAC-SHA256.

USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,23}$")
ADULT_PIN_RE = re.compile(r"^\d{3,8}$")
ENTRY_KEYS = {"n", "u", "l", "g", "r", "hc", "id"}
DATA_KEYS = {"version", "favorites", "hot", "removal", "dead",
             "myChannels", "hunted", "playlists", "adultPin", "appVersion"}


# ---- TV share links (QR to Muse, added 2026-11-07) ----
# The IPTV apps show a QR on the TV for a list that is too long to fit
# in a QR code itself. POST /api/share stores the text in memory under
# a short random code; GET /s/<code> renders it as a plain page the
# phone can copy from into Muse. No auth: the codes are random and the
# store only keeps the last 50 shares. Render's disk is ephemeral and
# the process restarts wipe it — shares are meant to be scanned at once.
_SHARE_STORE = {}
_SHARE_LOCK = threading.Lock()
_SHARE_MAX = 50
_SHARE_CODE_LEN = 6


class ShareRequest(BaseModel):
    text: str = ""


def _new_share_code() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(_SHARE_CODE_LEN))


@app.post("/api/share")
def share_text(req: ShareRequest):
    text = (req.text or "")[:200000]  # raised 2026-11-08: 20k cut a 759-channel TV share at 259 entries
    if not text.strip():
        raise HTTPException(status_code=400, detail="Empty text.")
    code = _new_share_code()
    with _SHARE_LOCK:
        while code in _SHARE_STORE:
            code = _new_share_code()
        _SHARE_STORE[code] = {"text": text, "ts": time.time()}
        while len(_SHARE_STORE) > _SHARE_MAX:
            oldest = min(_SHARE_STORE, key=lambda k: _SHARE_STORE[k]["ts"])
            del _SHARE_STORE[oldest]
    return {"code": code}


@app.get("/s/{code}", response_class=HTMLResponse)
def shared_page(code: str):
    with _SHARE_LOCK:
        item = _SHARE_STORE.get(code)
    if item is None:
        return HTMLResponse(
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            "<title>Share not found</title></head><body>"
            "<h1>Share not found</h1>"
            "<p>This link has expired or was never shared.</p>"
            "</body></html>",
            status_code=404)
    body = html.escape(item["text"])
    return HTMLResponse(
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Shared from your TV</title></head><body "
        "style=\"font-family:sans-serif;max-width:800px;margin:40px auto;"
        "padding:0 16px\">"
        "<h1>Shared from your TV</h1>"
        "<p>Copy this into Muse.</p>"
        "<pre style=\"white-space:pre-wrap;word-break:break-word;"
        "background:#f4f4f4;padding:16px;border-radius:8px\">"
        + body + "</pre></body></html>")


def _now_ms():
    return int(time.time() * 1000)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             salt, PBKDF2_ITERATIONS)
    return "pbkdf2-sha256$%d$%s$%s" % (PBKDF2_ITERATIONS, _b64(salt), _b64(dk))


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2-sha256":
            return False
        salt = base64.b64decode(salt_b64)
        want = base64.b64decode(hash_b64)
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                  salt, int(iters))
        return hmac.compare_digest(got, want)
    except Exception:
        return False


def clean_username(raw) -> str:
    """Lowercased, validated username. Raises ValueError on bad input."""
    if not isinstance(raw, str):
        raise ValueError("Username must be text.")
    name = raw.strip().lower()
    if not USERNAME_RE.match(name):
        raise ValueError(
            "Username must be 3-24 characters, start with a letter, and "
            "use only letters, numbers, _ and -.")
    return name


def _clean_url_list(value, field: str, limit: int):
    if not isinstance(value, list):
        raise ValueError("'%s' must be a list." % field)
    if len(value) > limit:
        raise ValueError("'%s' has too many entries." % field)
    out = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 2048:
            raise ValueError("'%s' must contain only text links." % field)
        out.append(item)
    return out


def _clean_entry(value, field: str):
    if not isinstance(value, dict):
        raise ValueError("'%s' entries must be objects." % field)
    if set(value.keys()) - ENTRY_KEYS:
        raise ValueError("'%s' has an unknown field." % field)
    name = value.get("n", "")
    url = value.get("u", "")
    if not isinstance(name, str) or not isinstance(url, str) or not url:
        raise ValueError("'%s' entries need a name and a link." % field)
    entry = {"n": name, "u": url,
             "l": value.get("l", "") if isinstance(value.get("l", ""), str) else "",
             "g": value.get("g", "") if isinstance(value.get("g", ""), str) else ""}
    if value.get("r") is True:
        entry["r"] = True
    hc = value.get("hc", "")
    if isinstance(hc, str) and hc:
        entry["hc"] = hc
    # Love's entries carry a tvg-id instead of a hunt category.
    eid = value.get("id", "")
    if isinstance(eid, str) and eid:
        entry["id"] = eid
    return entry


def _clean_entry_list(value, field: str, limit: int):
    if not isinstance(value, list):
        raise ValueError("'%s' must be a list." % field)
    if len(value) > limit:
        raise ValueError("'%s' has too many entries." % field)
    return [_clean_entry(e, field) for e in value]


def validate_data(raw):
    """Validate + clean a save payload. Returns the cleaned dict.
    Raises ValueError with a user-safe message, or HTTPException(413)."""
    if not isinstance(raw, dict):
        raise ValueError("Saved data must be an object.")
    if set(raw.keys()) - DATA_KEYS:
        raise ValueError("Saved data has an unknown field.")
    data = {"version": 1}
    data["favorites"] = _clean_url_list(raw.get("favorites", []), "favorites", 20000)
    data["hot"] = _clean_url_list(raw.get("hot", []), "hot", 20000)
    data["removal"] = _clean_url_list(raw.get("removal", []), "removal", 20000)
    data["dead"] = _clean_url_list(raw.get("dead", []), "dead", 20000)
    data["myChannels"] = _clean_entry_list(raw.get("myChannels", []), "myChannels", 5000)
    data["hunted"] = _clean_entry_list(raw.get("hunted", []), "hunted", 5000)
    playlists = raw.get("playlists", [])
    if not isinstance(playlists, list) or len(playlists) > 50:
        raise ValueError("'playlists' must be a list of at most 50.")
    clean_pls = []
    for pl in playlists:
        if not isinstance(pl, dict):
            raise ValueError("Playlists must be objects.")
        name = pl.get("name", "")
        url = pl.get("url", "")
        if not isinstance(name, str) or not name or not isinstance(url, str):
            raise ValueError("Each playlist needs a name and a link.")
        clean_pls.append({
            "name": name,
            "url": url,
            "channels": _clean_entry_list(pl.get("channels", []), "channels", 5000),
        })
    data["playlists"] = clean_pls
    pin = raw.get("adultPin", DEFAULT_ADULT_PIN)
    if not isinstance(pin, str) or not ADULT_PIN_RE.match(pin):
        raise ValueError("Adult code must be 3-8 digits.")
    data["adultPin"] = pin
    ver = raw.get("appVersion", "")
    data["appVersion"] = ver if isinstance(ver, str) else ""
    if len(json.dumps(data).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Saved data is too large.")
    return data


class UserStore:
    """One JSON record per user. Subclasses hide where it lives."""

    def get(self, username):
        raise NotImplementedError

    def create(self, username, record):
        """Atomic create. True = created, False = username already taken."""
        raise NotImplementedError

    def update(self, username, record):
        """Overwrite an existing record. False = record vanished."""
        raise NotImplementedError

    def name(self):
        raise NotImplementedError


class GitHubUserStore(UserStore):
    """Private GitHub repo, users/<username>.json, via the Contents API.

    Register is a PUT with NO sha: GitHub answers 422 if the file already
    exists, which is the atomic uniqueness check. Updates send the current
    blob sha; a stale sha gets 409 and the update is retried once after a
    fresh GET. Reads fetch via download_url when the inline base64 content
    is absent (files over 1 MB). Writes for one user are serialised with a
    per-user lock, per GitHub's own guidance for this endpoint.
    """

    API = "https://api.github.com"

    def __init__(self, token: str, repo: str):
        self.token = token
        self.repo = repo  # "owner/name"
        self._locks = {}
        self._locks_guard = threading.Lock()

    def name(self):
        return "github"

    def _lock(self, username: str):
        with self._locks_guard:
            lock = self._locks.get(username)
            if lock is None:
                lock = threading.Lock()
                self._locks[username] = lock
            return lock

    def _request(self, method: str, path: str, body=None):
        url = self.API + path
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=payload, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "NOVATER-IPTV-Users")
        if payload is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8"))
            except Exception:
                detail = {}
            return e.code, detail

    def _path(self, username: str):
        return "/repos/%s/contents/users/%s.json" % (self.repo, username)

    def _fetch(self, username: str):
        """Returns (record dict|None, sha|None). Raises on API errors."""
        status, body = self._request("GET", self._path(username))
        if status == 404:
            return None, None
        if status != 200:
            raise RuntimeError("GitHub read failed (%s): %s" % (
                status, body.get("message", "unknown error")))
        sha = body.get("sha")
        content = body.get("content")
        if isinstance(content, str) and content.strip():
            raw = base64.b64decode("".join(content.split()))
            return json.loads(raw.decode("utf-8")), sha
        # Over 1 MB GitHub omits inline content — fetch the raw file.
        dl = body.get("download_url")
        if not dl:
            raise RuntimeError("GitHub returned no content for the user file.")
        req = urllib.request.Request(dl)
        req.add_header("User-Agent", "NOVATER-IPTV-Users")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8")), sha

    def get(self, username: str):
        record, _sha = self._fetch(username)
        return record

    def create(self, username: str, record: dict):
        with self._lock(username):
            body = {
                "message": "Add user %s" % username,
                "content": _b64(json.dumps(record).encode("utf-8")),
            }
            status, resp = self._request("PUT", self._path(username), body)
            if status in (200, 201):
                return True
            if status == 422:
                # Almost always: the file already exists. Confirm with a
                # GET so a different validation error is not misread.
                existing, _ = self._fetch(username)
                if existing is not None:
                    return False
                raise RuntimeError("GitHub rejected the new user file: %s"
                                   % resp.get("message", "validation failed"))
            raise RuntimeError("GitHub create failed (%s): %s" % (
                status, resp.get("message", "unknown error")))

    def update(self, username: str, record: dict):
        with self._lock(username):
            for _attempt in range(2):
                _old, sha = self._fetch(username)
                if sha is None:
                    return False
                body = {
                    "message": "Update user %s data" % username,
                    "content": _b64(json.dumps(record).encode("utf-8")),
                    "sha": sha,
                }
                status, resp = self._request("PUT", self._path(username), body)
                if status == 200:
                    return True
                if status == 409:
                    continue  # Stale sha — re-fetch once and retry.
                raise RuntimeError("GitHub update failed (%s): %s" % (
                    status, resp.get("message", "unknown error")))
            return False


class LocalJsonUserStore(UserStore):
    """File-per-user store for LOCAL TESTING ONLY. Never use in production:
    Render's disk is ephemeral and everything here would be lost on the
    next restart or redeploy. Create uses O_EXCL so it is atomic."""

    def __init__(self, directory: str):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self._locks = {}
        self._locks_guard = threading.Lock()

    def name(self):
        return "local"

    def _lock(self, username: str):
        with self._locks_guard:
            lock = self._locks.get(username)
            if lock is None:
                lock = threading.Lock()
                self._locks[username] = lock
            return lock

    def _file(self, username: str):
        return os.path.join(self.directory, username + ".json")

    def get(self, username: str):
        try:
            with open(self._file(username), "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def create(self, username: str, record: dict):
        with self._lock(username):
            path = self._file(username)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                return False
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(record, f)
            return True

    def update(self, username: str, record: dict):
        with self._lock(username):
            path = self._file(username)
            if not os.path.exists(path):
                return False
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(record, f)
            os.replace(tmp, path)
            return True


def _build_store():
    mode = os.environ.get("IPTV_USER_STORE", "github").strip().lower()
    if mode == "local":
        return LocalJsonUserStore(
            os.environ.get("IPTV_USER_DATA_DIR", "/tmp/iptv-users"))
    token = os.environ.get("GITHUB_USER_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_USER_REPO", "").strip()
    if not token or "/" not in repo:
        return None  # Not configured — user endpoints answer 503.
    return GitHubUserStore(token, repo)


USER_STORE = _build_store()


def _require_store():
    if USER_STORE is None:
        raise HTTPException(
            status_code=503,
            detail="User accounts are not set up on the server yet.")
    return USER_STORE


def _check_password(password, stored_hash):
    if not isinstance(password, str) or not password:
        return False
    return verify_password(password, stored_hash)


class RegisterRequest(BaseModel):
    username: str
    password: str = ""


class LoginRequest(BaseModel):
    username: str
    password: str = ""


class SaveRequest(BaseModel):
    username: str
    password: str = ""
    data: dict = {}


class LoadRequest(BaseModel):
    username: str
    password: str = ""


class ChangePasswordRequest(BaseModel):
    username: str
    oldPassword: str = ""
    newPassword: str = ""


@app.get("/api/user/health")
def user_health():
    return {"status": "ok",
            "store": USER_STORE.name() if USER_STORE is not None else "unconfigured"}


@app.post("/api/user/register", status_code=201)
def user_register(req: RegisterRequest):
    store = _require_store()
    try:
        username = clean_username(req.username)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    password = req.password or ""
    if password == "":
        password = DEFAULT_PASSWORD  # Ron's order: 5555 for new accounts.
    elif len(password) < 4 or len(password) > 64:
        raise HTTPException(status_code=400,
                            detail="Password must be 4-64 characters.")
    now = _now_ms()
    record = {
        "username": username,
        "passwordHash": hash_password(password),
        "adultPin": DEFAULT_ADULT_PIN,
        "createdAt": now,
        "updatedAt": now,
        "data": None,
    }
    try:
        created = store.create(username, record)
    except Exception:
        raise HTTPException(status_code=502,
                            detail="Could not reach the user store. Try again.")
    if not created:
        raise HTTPException(status_code=409,
                            detail="That username is already taken.")
    return {"username": username, "adultPin": DEFAULT_ADULT_PIN}


@app.post("/api/user/login")
def user_login(req: LoginRequest):
    store = _require_store()
    try:
        username = clean_username(req.username)
    except ValueError:
        # Same answer as a wrong password: never reveal whether a name exists.
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    try:
        record = store.get(username)
    except Exception:
        raise HTTPException(status_code=502,
                            detail="Could not reach the user store. Try again.")
    if record is None or not _check_password(req.password,
                                             record.get("passwordHash", "")):
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    return {"username": username,
            "adultPin": record.get("adultPin", DEFAULT_ADULT_PIN),
            "updatedAt": record.get("updatedAt", 0)}


@app.post("/api/user/save")
def user_save(req: SaveRequest):
    store = _require_store()
    try:
        username = clean_username(req.username)
    except ValueError:
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    try:
        data = validate_data(req.data)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        record = store.get(username)
    except Exception:
        raise HTTPException(status_code=502,
                            detail="Could not reach the user store. Try again.")
    if record is None:
        raise HTTPException(status_code=404, detail="No account with that username.")
    if not _check_password(req.password, record.get("passwordHash", "")):
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    record["data"] = data
    record["adultPin"] = data.get("adultPin", record.get("adultPin", DEFAULT_ADULT_PIN))
    record["updatedAt"] = _now_ms()
    try:
        ok = store.update(username, record)
    except Exception:
        raise HTTPException(status_code=502,
                            detail="Could not reach the user store. Try again.")
    if not ok:
        raise HTTPException(status_code=404, detail="No account with that username.")
    return {"updatedAt": record["updatedAt"]}


@app.post("/api/user/load")
def user_load(req: LoadRequest):
    store = _require_store()
    try:
        username = clean_username(req.username)
    except ValueError:
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    try:
        record = store.get(username)
    except Exception:
        raise HTTPException(status_code=502,
                            detail="Could not reach the user store. Try again.")
    if record is None or not _check_password(req.password,
                                             record.get("passwordHash", "")):
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    if record.get("data") is None:
        raise HTTPException(status_code=404, detail="No saved data yet.")
    return {"data": record["data"], "updatedAt": record.get("updatedAt", 0)}


@app.post("/api/user/change-password")
def user_change_password(req: ChangePasswordRequest):
    store = _require_store()
    try:
        username = clean_username(req.username)
    except ValueError:
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    new = req.newPassword or ""
    if len(new) < 4 or len(new) > 64:
        raise HTTPException(status_code=400,
                            detail="New password must be 4-64 characters.")
    try:
        record = store.get(username)
    except Exception:
        raise HTTPException(status_code=502,
                            detail="Could not reach the user store. Try again.")
    if record is None or not _check_password(req.oldPassword,
                                             record.get("passwordHash", "")):
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    record["passwordHash"] = hash_password(new)
    record["updatedAt"] = _now_ms()
    try:
        ok = store.update(username, record)
    except Exception:
        raise HTTPException(status_code=502,
                            detail="Could not reach the user store. Try again.")
    if not ok:
        raise HTTPException(status_code=404, detail="No account with that username.")
    return {"username": username}
