import uuid

import pytest

from backend import main
from backend.transcript import TranscriptSegment


class FakeTranscriber:
    async def transcribe(self, media_path, model_name, use_gpu=False, initial_prompt=None):
        assert media_path.is_file()
        assert model_name == "paraformer-zh"
        assert use_gpu is False
        return {
            "segments": [TranscriptSegment(0.1, 2.4, "这是测试语音内容")],
            "language": "zh",
            "duration": 2.5,
        }


def new_task() -> dict:
    return {
        "task_id": str(uuid.uuid4()),
        "url": "https://v.douyin.com/example/",
        "language": "zh",
        "status": "queued",
        "step": "等待处理",
        "progress": 0,
        "error": None,
        "result": None,
        "created_at": "2026-10-08T00:00:00+00:00",
        "updated_at": "2026-10-08T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_remote_audio_reaches_local_transcription(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(main, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
    main.TRANSCRIPTS_DIR.mkdir()
    monkeypatch.setattr(main, "paraformer", FakeTranscriber())

    class Remote:
        async def prepare(self, url, target, on_status=None):
            on_status("running")
            target.write_bytes(b"audio")
            return target, {"title": "服务器音频", "duration": 2.5}

    class LocalDownloadMustNotRun:
        def __init__(self, *args):
            raise AssertionError("remote audio should avoid local downloading")

    monkeypatch.setattr(main, "remote", Remote())
    monkeypatch.setattr(main, "VideoProcessor", LocalDownloadMustNotRun)
    task = new_task()
    await main.run_task(task)

    assert task["status"] == "completed"
    assert task["result"]["source"] == "server"
    assert (tmp_path / task["task_id"] / "transcript.srt").is_file()
    assert (tmp_path / "transcripts" / "服务器音频【转录】.md").is_file()
    assert (tmp_path / "transcripts" / "服务器音频【转录】.srt").is_file()


@pytest.mark.asyncio
async def test_remote_failure_falls_back_to_local_downloader(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(main, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
    main.TRANSCRIPTS_DIR.mkdir()
    monkeypatch.setattr(main, "paraformer", FakeTranscriber())

    class Remote:
        async def prepare(self, *args, **kwargs):
            raise RuntimeError("unavailable")

    class LocalProcessor:
        def __init__(self, root):
            self.root = root

        async def get_video_info(self, url):
            return {"title": "本机音频", "duration": 2.5}

        async def download_audio(self, url, task_id):
            path = self.root / task_id / "audio.m4a"
            path.write_bytes(b"audio")
            return path

    monkeypatch.setattr(main, "remote", Remote())
    monkeypatch.setattr(main, "VideoProcessor", LocalProcessor)
    task = new_task()
    await main.run_task(task)

    assert task["status"] == "completed"
    assert task["result"]["source"] == "local"
    assert (tmp_path / "transcripts" / "本机音频【转录】.srt").is_file()
