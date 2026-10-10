"""有界后台采集；采集故障不改变 HTTP 转发结果。"""
from __future__ import annotations

import asyncio
import logging
from capture_core import utc_timestamp

logger = logging.getLogger(__name__)


class NullCapture:
    def enqueue(self, *args, **kwargs):
        pass

    def finish(self, **kwargs):
        pass


class CaptureManager:
    def __init__(self, root, factory, *, buffer_bytes, max_pending):
        self.root = root
        self.factory = factory
        self.buffer_bytes = buffer_bytes
        self.max_pending = max_pending
        self.tasks: dict[asyncio.Task, BackgroundCapture] = {}
        self.failures = 0
        self.skipped = 0
        self.completed = 0
        self.last_failure = None
        self.log_task = None

    async def log_failure(self, stage, error_type, capture_id):
        try:
            await asyncio.to_thread(logger.error,
                "Capture failed: stage=%s type=%s capture=%s; relay continues",
                stage, error_type, capture_id)
        except Exception:
            pass  # 日志后端故障不能中断转发；健康计数仍保留。

    def report_failure(self, stage, error, capture_id=None):
        self.failures += 1
        # 不记录异常消息或 traceback，避免意外输出正文、认证参数。
        self.last_failure = {"stage": stage, "error_type": type(error).__name__,
            "capture_id": capture_id, "at": utc_timestamp()}
        # 错误日志也可能落在慢盘上，且不能为故障洪峰创建无界任务。
        if self.log_task is None or self.log_task.done():
            self.log_task = asyncio.create_task(self.log_failure(stage, type(error).__name__, capture_id))

    def create(self):
        if len(self.tasks) >= self.max_pending:
            self.skipped += 1
            self.report_failure("capacity_limit", RuntimeError())
            return NullCapture()
        try:
            sink = BackgroundCapture(self, self.factory(self.root))
        except Exception as exc:
            self.report_failure("create", exc)
            return NullCapture()
        task = asyncio.create_task(sink.run())
        self.tasks[task] = sink
        task.add_done_callback(lambda done: self.tasks.pop(done, None))
        return sink

    def metrics(self):
        return {"status": "degraded" if self.failures else "ok", "active": len(self.tasks),
            "completed": self.completed, "failures": self.failures, "skipped": self.skipped,
            "pending_bytes": sum(s.pending_bytes for s in self.tasks.values()),
            "buffer_limit_bytes": self.buffer_bytes, "max_pending": self.max_pending,
            "last_failure": self.last_failure}

    async def close(self, timeout=5):
        tasks = list(self.tasks)
        if self.log_task is not None and not self.log_task.done():
            tasks.append(self.log_task)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            sink = self.tasks.get(task)
            if sink:
                sink.fail("shutdown_timeout", TimeoutError())


class BackgroundCapture:
    def __init__(self, manager, capture):
        self.manager = manager
        self.capture = capture
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self.pending_bytes = 0
        self.failed = None
        self.finished = False

    def clear_queue(self):
        while not self.queue.empty():
            item = self.queue.get_nowait()
            if item is not None:
                self.pending_bytes -= item[3]

    def fail(self, stage, error):
        if self.failed:
            return
        self.failed = f"{stage}: {type(error).__name__}"
        self.manager.report_failure(stage, error, self.capture.capture_id)
        self.clear_queue()
        self.queue.put_nowait(None)

    def enqueue(self, operation, *args, **kwargs):
        if self.failed or self.finished:
            return
        size = len(args[0]) if operation == "append_response" else len(kwargs.get("raw_body", b""))
        if self.pending_bytes + size > self.manager.buffer_bytes or self.queue.full():
            self.fail("buffer_overflow", BufferError())
            return
        self.pending_bytes += size
        self.queue.put_nowait((operation, args, kwargs, size))

    def finish(self, **kwargs):
        if self.finished:
            return
        self.enqueue("finalize", **kwargs)
        self.finished = True

    async def run(self):
        try:
            while True:
                item = await self.queue.get()
                if item is None or self.failed:
                    break
                method, args, kwargs, size = item
                try:
                    await asyncio.to_thread(getattr(self.capture, method), *args, **kwargs)
                except Exception as exc:
                    self.fail(method, exc)
                    break
                finally:
                    self.pending_bytes -= size
                if self.failed:
                    break
                if method == "finalize":
                    self.manager.completed += 1
                    return
            if self.failed:
                try:
                    await asyncio.to_thread(self.capture.fail_capture, self.failed)
                except Exception:
                    # 原始故障已进入健康状态和日志；磁盘损坏时无法保证标记落盘。
                    pass
        except asyncio.CancelledError:
            self.fail("worker_cancelled", asyncio.CancelledError())
            raise
        finally:
            self.clear_queue()
