"""Local transcription app: remote audio first, local download as fallback."""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .paraformer_asr import ParaformerTranscriber
from .remote_audio import RemoteAudioClient
from .transcript import segments_to_prompt, segments_to_srt, transcript_quality
from .video_processor import VideoProcessor, normalize_video_input
from .whisper_asr import WhisperTranscriber

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
WORKSPACE_DIR = Path(os.getenv("VIDEOTONOTES_WORKSPACE", BASE_DIR / "workspace")).resolve()
MODEL_DIR = Path(os.getenv("WHISPER_CACHE_DIR", WORKSPACE_DIR / "_model_cache")).resolve()
TRANSCRIPTS_DIR = WORKSPACE_DIR / "transcripts"
REMOTE_URL = os.getenv("URL2AUDIO_API_URL", "").strip()
REMOTE_TOKEN = os.getenv("URL2AUDIO_TOKEN", "").strip()
VERSION = "2.0.0"

WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
whisper = WhisperTranscriber(MODEL_DIR)
paraformer = ParaformerTranscriber(MODEL_DIR)
remote = RemoteAudioClient(REMOTE_URL, REMOTE_TOKEN) if REMOTE_URL and REMOTE_TOKEN else None
slots = asyncio.Semaphore(1)
tasks: dict[str, dict] = {}


class TranscribeRequest(BaseModel):
    video_url: str
    language: Literal["zh", "en"] = "zh"


def task_dir(task_id: str) -> Path:
    try:
        uuid.UUID(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="无效的任务编号") from exc
    return WORKSPACE_DIR / task_id


def save_task(task: dict) -> None:
    directory = task_dir(task["task_id"])
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "task.json"
    temporary = directory / "task.json.tmp"
    temporary.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def update_task(task: dict, **changes: object) -> None:
    task.update(changes)
    task["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_task(task)


def archive_stem(title: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title).strip(" .")[:72]
    return cleaned or "未命名视频"


def archive_files(directory: Path, title: str) -> tuple[Path, Path]:
    stem = archive_stem(title)
    candidate = TRANSCRIPTS_DIR / f"{stem}【转录】.md"
    suffix = 2
    while candidate.exists():
        candidate = TRANSCRIPTS_DIR / f"{stem}【转录】- {suffix}.md"
        suffix += 1
    srt = candidate.with_suffix(".srt")
    candidate.write_bytes((directory / "transcript.md").read_bytes())
    srt.write_bytes((directory / "transcript.srt").read_bytes())
    return candidate, srt


def restore_tasks() -> None:
    for path in WORKSPACE_DIR.glob("*/task.json"):
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
            if task.get("language") not in {"zh", "en"} or not task.get("task_id"):
                continue
            if task.get("status") in {"queued", "processing"}:
                task["status"] = "failed"
                task["error"] = "服务重启中断了任务，请重新提交链接"
                save_task(task)
            tasks[task["task_id"]] = task
        except (OSError, ValueError, KeyError):
            continue


@asynccontextmanager
async def lifespan(_: FastAPI):
    restore_tasks()
    yield


app = FastAPI(title="VideoToNo Transcription", lifespan=lifespan)


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "VideoToNo",
        "version": VERSION,
        "remote_audio": bool(remote),
    }


@app.get("/api/models")
def models() -> dict:
    return {
        "zh": paraformer.cache_status(),
        "en": whisper._model_cache_status("small"),
    }


@app.post("/api/transcribe", status_code=202)
async def submit(request: TranscribeRequest) -> dict[str, str]:
    url = normalize_video_input(request.video_url)
    if not url:
        raise HTTPException(status_code=422, detail="请提供有效的视频链接")
    VideoProcessor.detect_source(url)
    task_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    task = {
        "task_id": task_id,
        "url": url,
        "language": request.language,
        "status": "queued",
        "step": "等待处理",
        "progress": 0,
        "error": None,
        "result": None,
        "created_at": now,
        "updated_at": now,
    }
    tasks[task_id] = task
    save_task(task)
    asyncio.create_task(run_task(task))
    return {"task_id": task_id}


async def run_task(task: dict) -> None:
    async with slots:
        task_id = task["task_id"]
        directory = task_dir(task_id)
        url = task["url"]
        update_task(task, status="processing", step="准备音频", progress=10)
        try:
            media_path: Path | None = None
            info: dict = {}
            source = "local"
            if remote:
                try:
                    def remote_status(status: str) -> None:
                        update_task(task, step=f"服务器准备音频：{status}", progress=20)

                    media_path, info = await remote.prepare(
                        url, directory / "audio.m4a", on_status=remote_status
                    )
                    source = "server"
                except Exception as exc:
                    update_task(task, step=f"服务器未取到音频，改用本机下载：{exc}", progress=20)

            if media_path is None:
                processor = VideoProcessor(WORKSPACE_DIR)
                info = await processor.get_video_info(url)
                media_path = await processor.download_audio(url, task_id)

            title = str(info.get("title") or "视频转录").strip()
            duration = float(info.get("duration") or 0)
            model = "paraformer-zh" if task["language"] == "zh" else "small"
            update_task(task, step=f"本机转录：{model}", progress=55)
            if task["language"] == "zh":
                output = await paraformer.transcribe(media_path, model, use_gpu=False)
            else:
                def transcription_progress(current: float, total: float) -> None:
                    if total > 0:
                        update_task(
                            task,
                            step=f"本机转录：{int(current)} / {int(total)} 秒",
                            progress=55 + min(35, int(35 * current / total)),
                        )

                output = await whisper.transcribe(
                    media_path, model, use_gpu=False, initial_prompt=title[:120],
                    progress_callback=transcription_progress,
                )
            segments = output["segments"]
            if not segments:
                raise RuntimeError("没有识别到语音")
            quality = transcript_quality(segments, duration or float(output.get("duration") or 0))
            payload = {
                "language": output["language"],
                "model": model,
                "source": source,
                "quality": quality,
                "segments": [segment.to_dict() for segment in segments],
            }
            (directory / "transcript.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (directory / "transcript.md").write_text(
                "# 带时间戳转录\n\n" + segments_to_prompt(segments) + "\n",
                encoding="utf-8",
            )
            (directory / "transcript.srt").write_text(
                segments_to_srt(segments), encoding="utf-8"
            )
            markdown, subtitle = archive_files(directory, title)
            update_task(
                task,
                status="completed",
                step="转录完成",
                progress=100,
                result={
                    "title": title,
                    "duration": duration,
                    "language": output["language"],
                    "model": model,
                    "source": source,
                    "segments": len(segments),
                    "markdown": str(markdown),
                    "srt": str(subtitle),
                },
            )
        except Exception as exc:
            update_task(task, status="failed", step="处理失败", error=str(exc)[:500])


@app.get("/api/tasks")
def list_tasks() -> dict:
    ordered = sorted(tasks.values(), key=lambda task: task["created_at"], reverse=True)
    return {"tasks": ordered[:20]}


@app.get("/api/task/{task_id}")
def get_task(task_id: str) -> dict:
    task_dir(task_id)
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    return tasks[task_id]


@app.get("/api/task/{task_id}/transcript")
def get_transcript(task_id: str) -> dict:
    path = task_dir(task_id) / "transcript.json"
    if not path.is_file():
        raise HTTPException(status_code=409, detail="字幕尚未生成")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/task/{task_id}/file/{kind}")
def download_file(task_id: str, kind: Literal["srt", "md"]) -> FileResponse:
    task = get_task(task_id)
    if task["status"] != "completed":
        raise HTTPException(status_code=409, detail="字幕尚未生成")
    path = task_dir(task_id) / f"transcript.{kind}"
    title = archive_stem(task["result"]["title"])
    return FileResponse(path, filename=f"{title}【转录】.{kind}")


app.mount("/", StaticFiles(directory=BASE_DIR / "frontend", html=True), name="frontend")
