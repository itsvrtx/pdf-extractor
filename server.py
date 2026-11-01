#!/usr/bin/env python3
"""
PDF Extractor - web server
==========================
Zero-install front end for the extraction engine in pdf_extractor.py.
Users open a URL in their browser, paste a page address, and the SERVER runs
Playwright/Chromium, intercepts the PDFs and hands them back for Download / View.

    uvicorn server:app --host 0.0.0.0 --port 8000      (must stay at 1 worker: jobs live in memory)

Environment variables (all optional)
    ACCESS_TOKEN      If set, every API call needs this token. STRONGLY recommended on a public host.
    APP_TITLE         Branding text in the UI                          (default "PDF Extractor")
    DATA_DIR          Where job files are written                      (default ./data)
    MAX_CONCURRENT    Browsers running at once                         (default 2)
    MAX_QUEUE         Jobs allowed to wait before new ones get a 503   (default 8)
    RATE_PER_MIN      New jobs per client IP per minute                (default 6)
    JOB_TTL_MIN       Minutes before files are auto-deleted            (default 30)
    MAX_WAIT          Largest "listen" time a user may request, sec    (default 20)
    MAX_JOB_SECONDS   Hard time budget per job                         (default 120)
    MAX_PDF_MB        Largest PDF accepted                             (default 100)
    TRUST_PROXY       "1" to read the client IP from X-Forwarded-For   (set behind Render/Fly/Cloud Run)
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import secrets
import shutil
import socket
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from pdf_extractor import Extractor, PdfResult, inspect_pdf, resource_path, sanitize_filename, unlock_pdf

# ------------------------------------------------------------------ config
APP_TITLE = os.environ.get("APP_TITLE", "PDF Extractor")
DATA_DIR = Path(os.environ.get("DATA_DIR", "data")).resolve()
JOBS_DIR = DATA_DIR / "jobs"
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", 2))
MAX_QUEUE = int(os.environ.get("MAX_QUEUE", 8))
RATE_PER_MIN = int(os.environ.get("RATE_PER_MIN", 6))
JOB_TTL = int(os.environ.get("JOB_TTL_MIN", 30)) * 60
MAX_WAIT = int(os.environ.get("MAX_WAIT", 20))
MAX_JOB_SECONDS = int(os.environ.get("MAX_JOB_SECONDS", 120))
MAX_PDF_BYTES = int(os.environ.get("MAX_PDF_MB", 100)) * 1024 * 1024
TRUST_PROXY = os.environ.get("TRUST_PROXY") == "1"

ICO_PATH = resource_path("assets/app.ico")
LOGO_PNG = DATA_DIR / "logo.png"
INDEX_HTML = resource_path("web/index.html")


# ------------------------------------------------------------------ SSRF guard
# A hosted "fetch any URL" service must never be usable to reach the server's own
# network (cloud metadata endpoints, localhost admin ports, private LAN ranges).
_dns_cache: dict[str, tuple[float, bool]] = {}


def _ip_is_public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global


def is_public_url(url: str) -> bool:
    """True only for http(s) URLs whose host resolves exclusively to public IPs."""
    try:
        u = urlparse(url)
    except ValueError:
        return False
    if u.scheme in ("data", "blob", "about"):      # never touch the network
        return True
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    host = u.hostname.strip("[]").lower()
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return False
    hit = _dns_cache.get(host)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    try:
        infos = socket.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
        ok = bool(infos) and all(_ip_is_public(i[4][0]) for i in infos)
    except (socket.gaierror, ValueError):
        ok = False
    _dns_cache[host] = (time.time(), ok)
    return ok


# ------------------------------------------------------------------ jobs
@dataclass
class Job:
    id: str
    url: str
    dir: Path
    ip: str
    created: float = field(default_factory=time.time)
    status: str = "queued"                       # queued | running | done | error
    events: list = field(default_factory=list)
    files: dict[str, PdfResult] = field(default_factory=dict)
    unlock_attempts: dict[str, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def emit(self, type_: str, **data) -> None:
        with self.lock:
            self.events.append({"type": type_, **data})


jobs: dict[str, Job] = {}
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT, thread_name_prefix="extract")
_recent: dict[str, deque] = defaultdict(deque)   # ip -> timestamps of recent submissions


def file_payload(fid: str, r: PdfResult) -> dict:
    return {"id": fid, "name": r.best_path.name, "suggested_name": r.suggested_name, "size": r.size, "pages": r.pages, "source": r.source,
            "status": r.status, "encrypted": r.encrypted, "locked": r.locked}


def run_job(job: Job, deep: bool, wait: int) -> None:
    """Runs on a worker thread: one Playwright browser per job."""
    job.status = "running"
    job.emit("status", status="running")

    def on_result(r: PdfResult) -> None:
        fid = uuid.uuid4().hex[:12]
        job.files[fid] = r
        job.emit("result", file=file_payload(fid, r))

    ex = Extractor(
        out_dir=job.dir, headless=True, deep=deep, wait=wait,
        log=lambda m: job.emit("log", message=m), on_result=on_result,
        password_provider=None,                  # web flow: locked files are flagged, user unlocks via the UI
        url_guard=is_public_url, max_bytes=MAX_PDF_BYTES, max_seconds=MAX_JOB_SECONDS,
    )
    try:
        ex.run(job.url)
        job.status = "done"
    except Exception as exc:                     # noqa: BLE001 - surface any failure to the user
        job.emit("log", message=f"ERROR: {exc}")
        job.status = "error"
    job.emit("done", status=job.status, count=len(job.files))


# ------------------------------------------------------------------ auth / rate limit helpers
def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def require_token(request: Request, token: Optional[str] = Query(None)) -> None:
    if not ACCESS_TOKEN:
        return
    supplied = request.headers.get("x-access-token") or token or ""
    if not secrets.compare_digest(supplied.encode(), ACCESS_TOKEN.encode()):
        raise HTTPException(401, "Missing or invalid access token.")


def get_job(job_id: str) -> Job:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found or expired.")
    return job


def get_file(job: Job, fid: str) -> PdfResult:
    r = job.files.get(fid)
    if not r or not r.best_path.exists():
        raise HTTPException(404, "File not found.")
    return r


# ------------------------------------------------------------------ app
async def _janitor() -> None:
    """Delete finished jobs (and their PDFs) after JOB_TTL."""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for jid in [j for j, job in jobs.items() if now - job.created > JOB_TTL and job.status in ("done", "error")]:
            shutil.rmtree(jobs.pop(jid).dir, ignore_errors=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    shutil.rmtree(JOBS_DIR, ignore_errors=True)  # nothing survives a restart
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    task = asyncio.create_task(_janitor())
    yield
    task.cancel()
    executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title=APP_TITLE, lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"      # lets our own preview iframe work, nothing else
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


# ---- branding + pages
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(INDEX_HTML, media_type="text/html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    if ICO_PATH.exists():
        return FileResponse(ICO_PATH, media_type="image/x-icon",
                            headers={"Cache-Control": "public, max-age=86400"})
    return Response(status_code=204)


@app.get("/logo.png", include_in_schema=False)
def logo() -> Response:
    """UI logo, derived once from the same .ico used for the desktop build."""
    if not ICO_PATH.exists():
        return Response(status_code=404)
    try:
        if not LOGO_PNG.exists() or LOGO_PNG.stat().st_mtime < ICO_PATH.stat().st_mtime:
            from PIL import Image
            im = Image.open(ICO_PATH)
            im.size = max(im.info.get("sizes", {im.size}))   # pick the largest embedded size
            im.load()
            LOGO_PNG.parent.mkdir(parents=True, exist_ok=True)
            im.convert("RGBA").save(LOGO_PNG)
        return FileResponse(LOGO_PNG, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
    except Exception:                                         # noqa: BLE001
        return Response(status_code=404)


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"ok": True}


@app.get("/api/config")
def config() -> dict:
    return {"title": APP_TITLE, "token_required": bool(ACCESS_TOKEN), "has_logo": ICO_PATH.exists(),
            "max_wait": MAX_WAIT}


# ---- jobs API
class JobRequest(BaseModel):
    url: str = Field(min_length=3, max_length=2048)
    deep: bool = False
    wait: int = Field(default=6, ge=1)


@app.post("/api/jobs", dependencies=[Depends(require_token)])
def create_job(req: JobRequest, request: Request) -> dict:
    url = req.url.strip()
    if "://" not in url:
        url = "https://" + url
    if not is_public_url(url):
        raise HTTPException(400, "That address isn't a public http(s) URL.")

    ip, now = client_ip(request), time.time()
    hist = _recent[ip]
    while hist and now - hist[0] > 60:
        hist.popleft()
    if len(hist) >= RATE_PER_MIN:
        raise HTTPException(429, "Too many requests - please wait a minute.")
    if sum(1 for j in jobs.values() if j.status in ("queued", "running")) >= MAX_CONCURRENT + MAX_QUEUE:
        raise HTTPException(503, "Server is busy - try again shortly.")
    hist.append(now)

    jid = uuid.uuid4().hex
    job = Job(id=jid, url=url, dir=JOBS_DIR / jid, ip=ip)
    jobs[jid] = job
    job.emit("log", message="Queued...")
    executor.submit(run_job, job, req.deep, min(req.wait, MAX_WAIT))
    return {"id": jid}


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_token)])
def job_snapshot(job_id: str) -> dict:
    job = get_job(job_id)
    return {"id": job.id, "url": job.url, "status": job.status,
            "files": [file_payload(f, r) for f, r in job.files.items()]}


@app.get("/api/jobs/{job_id}/events", dependencies=[Depends(require_token)])
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    """Server-Sent Events: live log lines, each PDF as it is found, then 'done'."""
    job = get_job(job_id)

    async def stream():
        idx, last_ping = 0, time.time()
        while True:
            with job.lock:
                batch = job.events[idx:]
            for ev in batch:
                idx += 1
                yield f"data: {json.dumps(ev)}\n\n"
                if ev["type"] == "done":
                    return
            if await request.is_disconnected():
                return
            if time.time() - last_ping > 15:         # keep proxies from closing the stream
                yield ": ping\n\n"
                last_ping = time.time()
            await asyncio.sleep(0.3)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/jobs/{job_id}/files/{fid}/download", dependencies=[Depends(require_token)])
def download(job_id: str, fid: str, name: Optional[str] = Query(None, max_length=300)) -> FileResponse:
    """Classic download. `name` is the file name the user chose; it is re-sanitised here because
    client-side checks can never be trusted (path separators, control chars, reserved names...)."""
    r = get_file(get_job(job_id), fid)
    filename = sanitize_filename(name, r.suggested_name)
    return FileResponse(r.best_path, media_type="application/pdf", filename=filename,
                        content_disposition_type="attachment")


@app.get("/api/jobs/{job_id}/files/{fid}/view", dependencies=[Depends(require_token)])
def view(job_id: str, fid: str) -> FileResponse:
    """Inline, so the browser's built-in PDF viewer opens it for verification."""
    r = get_file(get_job(job_id), fid)
    return FileResponse(r.best_path, media_type="application/pdf", filename=r.suggested_name,
                        content_disposition_type="inline")


class UnlockRequest(BaseModel):
    password: str = Field(max_length=256)


@app.post("/api/jobs/{job_id}/files/{fid}/unlock", dependencies=[Depends(require_token)])
def unlock(job_id: str, fid: str, body: UnlockRequest) -> JSONResponse:
    """Uses a password the USER supplies - there is no guessing or cracking."""
    job = get_job(job_id)
    r = get_file(job, fid)
    if not r.locked:
        return JSONResponse(file_payload(fid, r))
    if job.unlock_attempts.get(fid, 0) >= 5:
        raise HTTPException(429, "Too many attempts for this file.")
    job.unlock_attempts[fid] = job.unlock_attempts.get(fid, 0) + 1
    out = unlock_pdf(r.path, body.password)
    if not out:
        raise HTTPException(403, "Wrong password.")
    r.unlocked_path, r.locked, r.status = out, False, "unlocked"
    r.pages = inspect_pdf(out)["pages"]
    r.refresh_suggestion()
    return JSONResponse(file_payload(fid, r))


@app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_token)])
def delete_job(job_id: str) -> dict:
    job = get_job(job_id)
    if job.status in ("queued", "running"):
        raise HTTPException(409, "Job still running.")
    shutil.rmtree(jobs.pop(job_id).dir, ignore_errors=True)
    return {"deleted": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
