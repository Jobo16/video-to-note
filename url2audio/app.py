"""Private URL-to-audio API. Transcription always stays on the Mac."""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import sqlite3
import subprocess
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.video_processor import VideoProcessor

DATA_DIR = Path(os.getenv("URL2AUDIO_DATA_DIR", "/data")).resolve()
DB_PATH = DATA_DIR / "jobs.sqlite3"
JOB_DIR = DATA_DIR / "jobs"
TOKEN = os.getenv("URL2AUDIO_TOKEN", "")
KEEP_SECONDS = 24 * 60 * 60
ALLOWED_HOSTS = (
    "douyin.com", "iesdouyin.com", "bilibili.com", "b23.tv",
    "youtube.com", "youtu.be",
)


class JobRequest(BaseModel):
    url: str


@contextmanager
def database():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_database() -> None:
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    with database() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                status TEXT NOT NULL,
                title TEXT,
                duration REAL,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )
        connection.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'")


def get_job(job_id: str) -> dict | None:
    with database() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def next_job() -> dict | None:
    with database() as connection:
        row = connection.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row:
            connection.execute(
                "UPDATE jobs SET status = 'running', updated_at = ? WHERE id = ?",
                (time.time(), row["id"]),
            )
    return dict(row) if row else None


def finish_job(job_id: str, *, title: str = "", duration: float = 0, error: str = "") -> None:
    with database() as connection:
        connection.execute(
            "UPDATE jobs SET status = ?, title = ?, duration = ?, error = ?, updated_at = ? WHERE id = ?",
            ("failed" if error else "ready", title, duration, error[:500], time.time(), job_id),
        )


def expire_jobs() -> None:
    cutoff = time.time() - KEEP_SECONDS
    with database() as connection:
        rows = connection.execute(
            "SELECT id FROM jobs WHERE status IN ('ready', 'failed') AND updated_at < ?",
            (cutoff,),
        ).fetchall()
        for row in rows:
            shutil.rmtree(JOB_DIR / row["id"], ignore_errors=True)
            connection.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))


def make_audio(source: Path, target: Path) -> None:
    temporary = target.with_name("ready.part.m4a")
    try:
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "aac", "-b:a", "64k", str(temporary),
            ],
            check=True,
            timeout=1800,
            capture_output=True,
        )
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError("音频提取结果为空")
        temporary.replace(target)
        source.unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)


async def worker() -> None:
    last_cleanup = 0.0
    while True:
        if time.monotonic() - last_cleanup > 300:
            await asyncio.to_thread(expire_jobs)
            last_cleanup = time.monotonic()
        job = await asyncio.to_thread(next_job)
        if not job:
            await asyncio.sleep(1)
            continue
        try:
            processor = VideoProcessor(JOB_DIR)
            info = await processor.get_video_info(job["url"])
            source = await processor.download_audio(job["url"], job["id"])
            await asyncio.to_thread(make_audio, source, JOB_DIR / job["id"] / "ready.m4a")
            await asyncio.to_thread(
                finish_job, job["id"], title=str(info.get("title") or ""),
                duration=float(info.get("duration") or 0),
            )
        except Exception as exc:
            await asyncio.to_thread(finish_job, job["id"], error=str(exc))


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not TOKEN:
        raise RuntimeError("URL2AUDIO_TOKEN is required")
    init_database()
    task = asyncio.create_task(worker())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="url2audio", docs_url=None, redoc_url=None, lifespan=lifespan)


def authorize(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {TOKEN}"
    if not TOKEN or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "url2audio"}


@app.post("/jobs", dependencies=[Depends(authorize)], status_code=202)
def submit_job(request: JobRequest) -> dict[str, str]:
    parsed = urlparse(request.url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(
        host == allowed or host.endswith(f".{allowed}") for allowed in ALLOWED_HOSTS
    ):
        raise HTTPException(status_code=422, detail="Unsupported video URL")
    job_id = str(uuid.uuid4())
    now = time.time()
    with database() as connection:
        connection.execute(
            "INSERT INTO jobs (id, url, status, created_at, updated_at) VALUES (?, ?, 'queued', ?, ?)",
            (job_id, request.url.strip(), now, now),
        )
    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}", dependencies=[Depends(authorize)])
def job_status(job_id: str) -> dict:
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    result = {key: job[key] for key in ("id", "status", "title", "duration", "error")}
    if job["status"] == "ready":
        result["size_bytes"] = (JOB_DIR / job_id / "ready.m4a").stat().st_size
    return result


@app.get("/jobs/{job_id}/audio", dependencies=[Depends(authorize)])
def job_audio(job_id: str) -> FileResponse:
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    path = JOB_DIR / job_id / "ready.m4a"
    if job["status"] != "ready" or not path.is_file():
        raise HTTPException(status_code=409, detail="Audio not ready")
    return FileResponse(path, media_type="audio/mp4", filename="audio.m4a")
