import time
from pathlib import Path

from fastapi.testclient import TestClient

import url2audio.app as server


def test_private_job_downloads_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "jobs.sqlite3")
    monkeypatch.setattr(server, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(server, "TOKEN", "test-token")

    class Processor:
        def __init__(self, root):
            self.root = root

        async def get_video_info(self, url):
            return {"title": "测试视频", "duration": 3}

        async def download_audio(self, url, job_id):
            directory = self.root / job_id
            directory.mkdir()
            source = directory / "audio.m4a"
            source.write_bytes(b"audio")
            return source

    def fake_make_audio(source: Path, target: Path):
        target.write_bytes(source.read_bytes())
        source.unlink()

    monkeypatch.setattr(server, "VideoProcessor", Processor)
    monkeypatch.setattr(server, "make_audio", fake_make_audio)

    with TestClient(server.app) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/jobs", json={"url": "https://v.douyin.com/test/"}).status_code == 401
        headers = {"Authorization": "Bearer test-token"}
        assert client.post(
            "/jobs", headers=headers, json={"url": "https://example.com/video"}
        ).status_code == 422
        submitted = client.post(
            "/jobs", headers=headers, json={"url": "https://v.douyin.com/test/"}
        )
        assert submitted.status_code == 202
        job_id = submitted.json()["job_id"]
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            job = client.get("/jobs/" + job_id, headers=headers).json()
            if job["status"] == "ready":
                break
            time.sleep(0.05)
        assert job["status"] == "ready"
        assert job["title"] == "测试视频"
        audio = client.get("/jobs/" + job_id + "/audio", headers=headers)
        assert audio.status_code == 200
        assert audio.content == b"audio"
