"""采集故障不应改变真实 HTTP 转发，也不能生成伪完整训练轮次。"""
import asyncio
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from capture_core import RequestCapture
from proxy import Settings, create_app


WIRE = b'data: {"id":"isolation","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'


class CaptureIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_error_logging_does_not_block_response_or_health(self):
        started = threading.Event()
        release = threading.Event()
        def slow_log(*args):
            started.set()
            release.wait(5)
        with tempfile.TemporaryDirectory() as temporary:
            async def upstream(_):
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=httpx.ByteStream(WIRE))
            app = create_app(Settings("127.0.0.1", 0, "http://upstream", Path(temporary), 30), httpx.MockTransport(upstream))
            with patch.object(RequestCapture, "append_response", side_effect=OSError("no space")), patch("capture_sink.logger.error", slow_log):
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
                        try:
                            response = await asyncio.wait_for(client.post("/v1/chat/completions", json={"messages": []}), 1)
                            self.assertEqual(response.content, WIRE)
                            for _ in range(100):
                                if started.is_set():break
                                await asyncio.sleep(0.01)
                            self.assertTrue(started.is_set())
                            health = await asyncio.wait_for(client.get("/healthz"), 1)
                            self.assertEqual(health.json()["capture"]["failures"], 1)
                        finally:
                            release.set()

    async def test_shutdown_timeout_marks_pending_capture_partial(self):
        started = threading.Event()
        release = threading.Event()
        original = RequestCapture.append_response
        def slow(capture, chunk):
            started.set()
            release.wait(5)
            return original(capture, chunk)
        with tempfile.TemporaryDirectory() as temporary:
            async def upstream(_):
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=httpx.ByteStream(WIRE))
            app = create_app(Settings("127.0.0.1", 0, "http://upstream", Path(temporary), 30), httpx.MockTransport(upstream))
            with patch.object(RequestCapture, "append_response", slow):
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
                        try:
                            response = await client.post("/v1/chat/completions", json={"messages": []})
                            self.assertEqual(response.content, WIRE)
                            for _ in range(100):
                                if started.is_set():break
                                await asyncio.sleep(0.01)
                            self.assertTrue(started.is_set())
                            await asyncio.wait_for(app.state.capture_manager.close(timeout=0.01), 1)
                            self.assertEqual(app.state.capture_manager.metrics()["last_failure"]["stage"], "shutdown_timeout")
                        finally:
                            release.set()
            states = list(Path(temporary).glob("raw/inflight/*/state.json"))
            self.assertEqual(len(states), 1)
            self.assertEqual(json.loads(states[0].read_text())["state"], "partial")
            self.assertEqual(app.state.capture_manager.metrics()["completed"], 0)

    async def test_next_request_can_capture_after_transient_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            async def upstream(_):
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=httpx.ByteStream(WIRE))
            app = create_app(Settings("127.0.0.1", 0, "http://upstream", Path(temporary), 30), httpx.MockTransport(upstream))
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
                    with patch.object(RequestCapture, "append_response", side_effect=OSError("disk temporarily unavailable")):
                        first = await client.post("/v1/chat/completions", json={"messages": []})
                        await app.state.capture_manager.close()
                    second = await client.post("/v1/chat/completions", json={"messages": []})
                    self.assertEqual(first.content, WIRE)
                    self.assertEqual(second.content, WIRE)
            self.assertEqual(app.state.capture_manager.metrics()["failures"], 1)
            self.assertEqual(app.state.capture_manager.metrics()["completed"], 1)
            self.assertEqual(len(list(Path(temporary).glob("raw/completed/*/response.body"))), 1)

    async def test_tcp_stream_and_health_continue_during_blocked_capture(self):
        import uvicorn
        started = threading.Event()
        release = threading.Event()
        upstream_finish = asyncio.Event()
        original = RequestCapture.append_response
        split = WIRE.index(b"data: [DONE]")
        def slow(capture, chunk):
            started.set()
            release.wait(5)
            return original(capture, chunk)
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield WIRE[:split]
                await upstream_finish.wait()
                yield WIRE[split:]
        with tempfile.TemporaryDirectory() as temporary:
            async def upstream(_):
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())
            app = create_app(Settings("127.0.0.1", 0, "http://upstream", Path(temporary), 30), httpx.MockTransport(upstream))
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            endpoint = f"http://127.0.0.1:{sock.getsockname()[1]}"
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            with patch.object(RequestCapture, "append_response", slow):
                task = asyncio.create_task(server.serve(sockets=[sock]))
                try:
                    for _ in range(100):
                        if server.started:
                            break
                        await asyncio.sleep(0.01)
                    self.assertTrue(server.started)
                    async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                        async with client.stream("POST", endpoint + "/v1/chat/completions", json={"messages": []}) as response:
                            chunks = response.aiter_raw()
                            first = await asyncio.wait_for(anext(chunks), 1)
                            self.assertEqual(first, WIRE[:split])
                            for _ in range(100):
                                if started.is_set():
                                    break
                                await asyncio.sleep(0.01)
                            self.assertTrue(started.is_set())
                            health = await asyncio.wait_for(client.get(endpoint + "/healthz"), 1)
                            self.assertEqual(health.status_code, 200)
                            self.assertGreater(health.json()["capture"]["active"], 0)
                            release.set()
                            upstream_finish.set()
                            rest = b"".join([chunk async for chunk in chunks])
                            self.assertEqual(first + rest, WIRE)
                finally:
                    release.set()
                    upstream_finish.set()
                    server.should_exit = True
                    await asyncio.wait_for(task, 5)
                    sock.close()
            captures = list(Path(temporary).glob("raw/completed/*/response.body"))
            self.assertEqual(len(captures), 1)
            self.assertEqual(captures[0].read_bytes(), WIRE)

    async def exchange(self, root, *, stream=True, status=200, **settings):
        body = WIRE if stream else b'{ "answer": "ok" }'
        seen = {}

        async def upstream(request):
            seen["body"] = await request.aread()
            seen["auth"] = request.headers["authorization"]
            return httpx.Response(status, headers=[
                ("content-type", "text/event-stream" if stream else "application/json"),
                ("x-test", "first"), ("x-test", "second")], stream=httpx.ByteStream(body))

        app = create_app(Settings("127.0.0.1", 30303, "http://upstream", root, 30, **settings), httpx.MockTransport(upstream))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
                response = await client.post("/v1/chat/completions", content=b'{ "messages": [] }',
                    headers={"authorization": "Bearer test-only"})
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.content, body)
        self.assertEqual(response.headers.get_list("x-test"), ["first", "second"])
        self.assertEqual(seen, {"body": b'{ "messages": [] }', "auth": "Bearer test-only"})
        return app

    async def test_disk_failure_does_not_interrupt_response(self):
        for stream in (False, True):
            for stage in ("start_request", "start_response", "append_response", "finalize"):
                with self.subTest(stream=stream, stage=stage), tempfile.TemporaryDirectory() as temporary:
                    with patch.object(RequestCapture, stage, side_effect=OSError("no space left")):
                        app = await self.exchange(Path(temporary), stream=stream)
                    self.assertEqual(app.state.capture_manager.metrics()["failures"], 1)
                    for state in Path(temporary).glob("raw/**/state.json"):
                        self.assertNotEqual(json.loads(state.read_text())["state"], "complete")

    async def test_initialization_failure_does_not_prevent_relay(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("proxy.initialize_capture_root", side_effect=PermissionError("read-only filesystem")):
                app = await self.exchange(Path(temporary))
            self.assertEqual(app.state.capture_manager.metrics()["status"], "degraded")

    async def test_parser_exception_does_not_interrupt_response(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("capture_core.SSEDecoder.feed", side_effect=RuntimeError("unexpected parse failure")):
                app = await self.exchange(Path(temporary))
            self.assertEqual(app.state.capture_manager.metrics()["failures"], 1)
            states = list(Path(temporary).glob("raw/inflight/*/state.json"))
            self.assertEqual(len(states), 1)
            self.assertEqual(json.loads(states[0].read_text())["state"], "partial")

    async def test_capture_failure_preserves_upstream_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(RequestCapture, "append_response", side_effect=OSError("no space left")):
                await self.exchange(Path(temporary), stream=False, status=429)

    async def test_slow_capture_does_not_block_stream_or_health(self):
        started = threading.Event()
        release = threading.Event()
        original = RequestCapture.append_response

        def slow(capture, chunk):
            started.set()
            release.wait(5)
            return original(capture, chunk)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            async def upstream(_):
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=httpx.ByteStream(WIRE))
            app = create_app(Settings("127.0.0.1", 30303, "http://upstream", root, 30), httpx.MockTransport(upstream))
            with patch.object(RequestCapture, "append_response", slow):
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
                        try:
                            response = await asyncio.wait_for(client.post("/v1/chat/completions", json={"messages": []}), 1)
                            self.assertEqual(response.content, WIRE)
                            for _ in range(100):
                                if started.is_set():
                                    break
                                await asyncio.sleep(0.01)
                            self.assertTrue(started.is_set())
                            health = await asyncio.wait_for(client.get("/healthz"), 1)
                            self.assertEqual(health.status_code, 200)
                            self.assertGreater(health.json()["capture"]["active"], 0)
                        finally:
                            release.set()
            self.assertEqual(app.state.capture_manager.metrics()["completed"], 1)

    async def test_capture_buffer_overflow_keeps_relay_and_marks_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = await self.exchange(Path(temporary), capture_buffer_bytes=1)
            self.assertEqual(app.state.capture_manager.metrics()["failures"], 1)
            self.assertEqual(app.state.capture_manager.metrics()["last_failure"]["stage"], "buffer_overflow")

    async def test_capture_capacity_limit_does_not_reject_requests(self):
        started = threading.Event()
        release = threading.Event()
        original = RequestCapture.start_request
        def slow(capture, **kwargs):
            started.set()
            release.wait(5)
            return original(capture, **kwargs)
        with tempfile.TemporaryDirectory() as temporary:
            async def upstream(_):
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=httpx.ByteStream(WIRE))
            app = create_app(Settings("127.0.0.1", 30303, "http://upstream", Path(temporary), 30,
                capture_max_pending=1), httpx.MockTransport(upstream))
            with patch.object(RequestCapture, "start_request", slow):
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
                        try:
                            first = await asyncio.wait_for(client.post("/v1/chat/completions", json={"messages": []}), 1)
                            second = await asyncio.wait_for(client.post("/v1/chat/completions", json={"messages": []}), 1)
                            self.assertEqual(first.content, WIRE)
                            self.assertEqual(second.content, WIRE)
                            self.assertEqual(app.state.capture_manager.metrics()["skipped"], 1)
                        finally:
                            release.set()
