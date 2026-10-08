"""Read the public video detail returned while Chromium loads a Douyin page."""

from __future__ import annotations

import os
from typing import Any

BROWSER_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/chromium",
    "/usr/bin/google-chrome",
)


def find_browser() -> str | None:
    return next((path for path in BROWSER_PATHS if os.path.isfile(path)), None)


def capture_detail(url: str, cookies: dict[str, str] | None = None) -> dict[str, Any]:
    browser_path = find_browser()
    if not browser_path:
        raise RuntimeError("未找到 Chrome、Edge 或 Chromium，无法读取抖音网页视频信息")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 Playwright；macOS 请运行 ./start-macos.sh 完成安装") from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=browser_path,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
            if os.getenv("URL2AUDIO_CONTAINER") == "1" else [],
        )
        try:
            context = browser.new_context()
            if cookies:
                context.add_cookies([
                    {"name": name, "value": value, "domain": ".douyin.com", "path": "/"}
                    for name, value in cookies.items() if value
                ])
            page = context.new_page()
            with page.expect_response(
                lambda response: "/aweme/v1/web/aweme/detail/" in response.url,
                timeout=60_000,
            ) as response_info:
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            payload = response_info.value.json()
            detail = payload.get("aweme_detail") if isinstance(payload, dict) else None
            if not isinstance(detail, dict) or not isinstance(detail.get("video"), dict):
                raise RuntimeError("抖音网页没有返回可用的视频详情")
            return detail
        finally:
            browser.close()
