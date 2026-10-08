"""Start the local transcription UI on macOS."""

from __future__ import annotations

import threading
import time
import urllib.request
import webbrowser

import uvicorn

from backend.main import app

URL = "http://127.0.0.1:8000"


def healthy() -> bool:
    try:
        with urllib.request.urlopen(URL + "/api/health", timeout=1) as response:
            return response.status == 200
    except Exception:
        return False


def main() -> None:
    if healthy():
        webbrowser.open(URL)
        return
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="info")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if healthy():
            print("VideoToNo 已启动：" + URL, flush=True)
            webbrowser.open(URL)
            break
        if not thread.is_alive():
            raise RuntimeError("本地服务启动失败")
        time.sleep(0.2)
    else:
        server.should_exit = True
        raise RuntimeError("本地服务启动超时")
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        server.should_exit = True
        thread.join()


if __name__ == "__main__":
    main()
