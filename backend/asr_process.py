"""把语音转写放进子进程跑。

faster-whisper（CT2）与 sherpa-onnx 都是 C++ 库，在打包环境里一旦访问违例，
崩掉的是整个进程——托盘、界面、正在排队的任务一起没，而 Python 层连一行日志都
来不及写（2026-09-30 本机实测：belle-turbo-zh / small 都在加载模型后约 3 秒
静默消失，同一份模型和同一段音频在源码环境跑得好好的）。

这里把转写隔离成一个工人进程：协议是 stdin 收一行任务 JSON，stdout 回 NDJSON
事件；工人 native 崩掉时主程序只是把这次任务报成失败。
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from .transcript import TranscriptSegment

# 工人被取消后，主程序最多再等这么久就硬收
CANCEL_GRACE_SECONDS = 60.0


def child_command() -> list[str]:
    """工人怎么起：打包版复用自身（带 --asr-child），源码版直接跑本模块。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--asr-child"]
    return [sys.executable, "-m", "backend.asr_process"]


def use_child_process() -> bool:
    if getattr(sys, "frozen", False):
        return True
    return False


def encode_result(result: dict[str, Any]) -> dict[str, Any]:
    data = dict(result)
    data["segments"] = [
        seg.to_dict() if isinstance(seg, TranscriptSegment) else dict(seg)
        for seg in (result.get("segments") or [])
    ]
    return data


def decode_result(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload)
    segments = []
    for seg in payload.get("segments") or []:
        if isinstance(seg, TranscriptSegment):
            segments.append(seg)
        else:
            segments.append(
                TranscriptSegment(
                    float(seg.get("start") or 0.0),
                    float(seg.get("end") or 0.0),
                    str(seg.get("text") or ""),
                )
            )
    data["segments"] = segments
    return data


class TranscriptionWorkerDied(RuntimeError):
    """工人没能正常交回结果：崩溃、被系统杀掉或自己退出。"""


def format_exit_code(code: int | None) -> str:
    """Windows 上 native 崩溃回的是 NTSTATUS（如 3221225477 = 0xC0000005），按十六进制才读得懂。"""
    if code is None:
        return "未知"
    if code < 0 or code >= 0xC0000000:
        return f"0x{code & 0xFFFFFFFF:08X}"
    return str(code)


async def run_in_child(
    media_path: Path,
    model_name: str,
    use_gpu: bool,
    title: str | None,
    cancel_event: Any = None,
    progress_callback: Callable[[float, float], None] | None = None,
) -> dict[str, Any]:
    """在工人进程里转写一个文件，事件照原样回传给主程序的回调。"""
    job = {
        "media_path": str(media_path),
        "model_name": model_name,
        "use_gpu": bool(use_gpu),
        "title": title,
    }
    process = await asyncio.create_subprocess_exec(
        *child_command(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        # stderr 不接管道：让它照原样进 _app.log，native 库的唠叨不会撑满管道把工人卡死
        stderr=None,
    )
    assert process.stdin and process.stdout
    writer = process.stdin
    writer.write((json.dumps(job, ensure_ascii=False) + "\n").encode("utf-8"))
    await writer.drain()

    events: asyncio.Queue[str] = asyncio.Queue()

    async def pump() -> None:
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            await events.put(line.decode("utf-8", "replace"))
        # 管道关了就说明工人没了（崩溃、被杀或自己退出）：用哨兵把等待的一方叫醒
        await events.put("")

    pump_task = asyncio.create_task(pump())
    cancel_task = asyncio.create_task(
        _send_cancel_when_asked(process, writer, cancel_event)
    )

    payload: dict[str, Any] | None = None
    failure: str | None = None
    cancelled = False
    try:
        while True:
            line = await events.get()
            if not line:
                break
            event = _parse_event(line)
            if event is None:
                continue
            kind = event.get("event")
            if kind == "progress":
                if progress_callback:
                    progress_callback(
                        float(event.get("done") or 0.0), float(event.get("total") or 0.0)
                    )
            elif kind == "cancelled":
                cancelled = True
                break
            elif kind == "done":
                payload = event.get("result") if isinstance(event.get("result"), dict) else {}
                break
            elif kind == "error":
                failure = str(event.get("message") or "转写工人未给出原因")
                break
    finally:
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)

    exit_code = await _reap(process, cancelled)
    pump_task.cancel()
    await asyncio.gather(pump_task, return_exceptions=True)

    if cancelled:
        from .whisper_asr import TranscriptionCancelledError

        raise TranscriptionCancelledError("转写已取消")
    if payload is None:
        reason = failure or f"退出码 {format_exit_code(exit_code)}"
        raise TranscriptionWorkerDied(
            f"语音转写进程异常退出（{reason}）；本次任务已停止，程序本身仍在运行，"
            "可换一个转写档位重试"
        )
    return decode_result(payload)


async def _send_cancel_when_asked(
    process: asyncio.subprocess.Process, writer: asyncio.StreamWriter, cancel_event: Any
) -> None:
    if cancel_event is None:
        return
    while process.returncode is None:
        if cancel_event.is_set():
            try:
                writer.write(b'{"cmd": "cancel"}\n')
                await writer.drain()
            except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
                return
            return
        await asyncio.sleep(0.5)


async def _reap(process: asyncio.subprocess.Process, cancelled: bool) -> int | None:
    if process.returncode is not None:
        return process.returncode
    try:
        if cancelled:
            await asyncio.wait_for(process.wait(), timeout=CANCEL_GRACE_SECONDS)
        else:
            await process.wait()
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
    return process.returncode


def _parse_event(line: str) -> dict[str, Any] | None:
    text = line.strip()
    if not text or not text.startswith("{"):
        return None
    try:
        event = json.loads(text)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


# --------------------------------------------------------------------------
# 工人这一侧
# --------------------------------------------------------------------------

_write_lock = threading.Lock()


def _emit(payload: dict[str, Any]) -> None:
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with _write_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def child_main() -> int:
    """读一行任务，转写完把结果发回 stdout。由 `--asr-child` 或 `-m backend.asr_process` 进入。"""
    from .main import run_transcription_local
    from .whisper_asr import TranscriptionCancelledError

    # 打包版是 noconsole 应用：管道句柄是主程序给的，但 Python 不一定替我们建好流对象
    if sys.stdout is None:
        sys.stdout = open(1, "w", encoding="utf-8", buffering=1, closefd=False)
    if sys.stdin is None:
        sys.stdin = open(0, "r", encoding="utf-8", closefd=False)

    job_line = sys.stdin.readline()
    if not job_line.strip():
        _emit({"event": "error", "message": "转写工人没有收到任务"})
        return 2
    try:
        job = json.loads(job_line)
    except ValueError as exc:
        _emit({"event": "error", "message": f"任务描述无法解析：{exc}"})
        return 2

    cancel_event = threading.Event()

    def watch_cancel() -> None:
        for line in sys.stdin:
            event = _parse_event(line)
            if event and event.get("cmd") == "cancel":
                cancel_event.set()
                return

    threading.Thread(target=watch_cancel, daemon=True).start()

    def report(done: float, total: float) -> None:
        _emit({"event": "progress", "done": done, "total": total})

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            run_transcription_local(
                Path(str(job.get("media_path") or "")),
                str(job.get("model_name") or "base"),
                bool(job.get("use_gpu")),
                job.get("title"),
                cancel_event,
                report,
            )
        )
    except TranscriptionCancelledError:
        _emit({"event": "cancelled"})
        return 3
    except Exception as exc:
        _emit({"event": "error", "message": f"{type(exc).__name__}: {exc}"})
        return 1
    finally:
        loop.close()
    _emit({"event": "done", "result": encode_result(result)})
    return 0


if __name__ == "__main__":
    raise SystemExit(child_main())
