"""Fetch prepared audio from the private url2audio service."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable


class RemoteAudioError(RuntimeError):
    pass


class RemoteAudioClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _request(self, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise RemoteAudioError(f"音频服务返回 HTTP {exc.code}") from exc

    def _download(self, job_id: str, target: Path) -> None:
        temporary = target.with_suffix(target.suffix + ".part")
        request = urllib.request.Request(
            f"{self.base_url}/jobs/{job_id}/audio",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                with temporary.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
            if temporary.stat().st_size == 0:
                raise RemoteAudioError("服务器返回空音频")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    async def prepare(
        self,
        url: str,
        target: Path,
        on_status: Callable[[str], None] | None = None,
    ) -> tuple[Path, dict]:
        job = await asyncio.to_thread(self._request, "/jobs", {"url": url})
        job_id = str(job["job_id"])
        deadline = time.monotonic() + 600
        last_status = ""
        while time.monotonic() < deadline:
            state = await asyncio.to_thread(self._request, f"/jobs/{job_id}")
            status = str(state.get("status") or "")
            if status != last_status and on_status:
                on_status(status)
            last_status = status
            if status == "ready":
                target.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(self._download, job_id, target)
                return target, {
                    "title": str(state.get("title") or ""),
                    "duration": float(state.get("duration") or 0),
                }
            if status == "failed":
                raise RemoteAudioError(str(state.get("error") or "服务器取音频失败"))
            await asyncio.sleep(2)
        raise RemoteAudioError("服务器取音频超时")
