"""归档、恢复、持久化清理条件及定时任务对代理的隔离。"""
import asyncio
import hashlib
import io
import json
import os
import sqlite3
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

import httpx

from capture_archive import (archive_completed,
                             restore_archives, snapshot, verify_archive)
from capture_core import RequestCapture
from export_sharegpt_recovered import export_sharegpt_recovered
from finalize import finalize_dataset
from proxy import Settings, create_app, parse_args


def make_capture(root, identifier="cap_one", *, partial=False):
    item = RequestCapture(root, identifier)
    item.start_request(method="POST", path="/v1/chat/completions", query="",
        url="http://proxy/v1/chat/completions", headers=[],
        raw_body=json.dumps({"model": "test-model", "messages": [{"role": "user", "content": "你好"}]}).encode(),
        upstream_url="http://upstream/v1/chat/completions", client_host=None, client_port=None)
    item.start_response(status_code=200, headers=[("content-type", "application/json")], is_sse=False)
    response = {"id": identifier, "choices": [{"index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": "世界", "reasoning_content": "想一下"}}]}
    item.append_response(json.dumps(response, ensure_ascii=False).encode())
    item.finalize(transport_error="interrupted" if partial else None)
    return item.paths.completed_dir


def run_archive(root, destination, **kwargs):
    return archive_completed(root, destination, min_age_seconds=kwargs.pop("min_age_seconds", 0),
        batch_size=kwargs.pop("batch_size", 100), delete_source=kwargs.pop("delete_source", False), **kwargs)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.raw = self.base / "captures/raw"
        self.archives = self.base / "archives"

    def archive_files(self):
        return list(self.archives.rglob("*.tar.gz"))

    def test_roundtrip_then_finalize_and_export(self):
        capture = make_capture(self.raw)
        original = snapshot(capture)
        result = run_archive(self.raw, self.archives, delete_source=True)
        self.assertEqual((result["archived_captures"], result["removed_captures"]), (1, 1))
        self.assertFalse(capture.exists())
        restored = self.base / "restored/raw"
        self.assertEqual(restore_archives(self.archive_files(), restored), 1)
        self.assertEqual(snapshot(restored / "completed/cap_one"), original)
        session = self.base / "sessions/s/wire.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(json.dumps({"message": {"type": "StatusUpdate", "payload": {"message_id": "cap_one"}}}) + "\n")
        report = finalize_dataset(restored.parent, session.parent.parent, self.base / "dataset", client="kimi")
        self.assertEqual(report["matched"], 1)
        export_sharegpt_recovered(self.base / "dataset", self.base / "export", "separate")
        exported = (self.base / "export/sharegpt.jsonl").read_text()
        self.assertIn("世界", exported)
        self.assertIn("想一下", exported)

    def test_copy_is_idempotent_and_restart_can_clean(self):
        capture = make_capture(self.raw)
        self.assertEqual(run_archive(self.raw, self.archives)["archived_captures"], 1)
        self.assertTrue(capture.exists())
        self.assertEqual(run_archive(self.raw, self.archives)["archived_captures"], 0)
        result = run_archive(self.raw, self.archives, delete_source=True)
        self.assertEqual((result["archived_captures"], result["removed_captures"]), (0, 1))
        self.assertEqual(len(self.archive_files()), 1)
        self.assertFalse(capture.exists())

    def test_missing_archive_is_recreated_before_cleaning(self):
        capture = make_capture(self.raw)
        run_archive(self.raw, self.archives)
        self.archive_files()[0].unlink()
        self.assertEqual(run_archive(self.raw, self.archives, delete_source=True)["archived_captures"], 1)
        self.assertFalse(capture.exists())
        verify_archive(self.archive_files()[0])

    def test_corrupt_existing_archive_keeps_source(self):
        capture = make_capture(self.raw)
        before = snapshot(capture)
        run_archive(self.raw, self.archives)
        self.archive_files()[0].write_bytes(b"broken archive")
        with self.assertRaises(tarfile.ReadError):
            run_archive(self.raw, self.archives, delete_source=True)
        self.assertEqual(snapshot(capture), before)

    def test_source_change_never_deleted_using_old_archive(self):
        capture = make_capture(self.raw)
        run_archive(self.raw, self.archives)
        (capture / "response.body").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            run_archive(self.raw, self.archives, delete_source=True)
        self.assertEqual((capture / "response.body").read_bytes(), b"changed")

    def test_verification_failure_never_publishes_or_deletes(self):
        capture = make_capture(self.raw)
        with patch("capture_archive.verify_archive", side_effect=ValueError("broken")):
            with self.assertRaises(ValueError):
                run_archive(self.raw, self.archives, delete_source=True)
        self.assertTrue(capture.exists())
        self.assertEqual(self.archive_files(), [])
        self.assertEqual(list(self.archives.rglob("*.tmp")), [])

    def test_index_commit_failure_keeps_source(self):
        (self.raw / "completed").mkdir(parents=True)
        run_archive(self.raw, self.archives)
        with closing(sqlite3.connect(self.archives / "index.sqlite3")) as index, index:
            index.execute("CREATE TRIGGER reject_capture BEFORE INSERT ON captures BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        capture = make_capture(self.raw)
        with self.assertRaises(sqlite3.IntegrityError):
            run_archive(self.raw, self.archives, delete_source=True)
        self.assertTrue(capture.exists())
        self.assertEqual(len(self.archive_files()), 1)
        verify_archive(self.archive_files()[0])

    def test_partial_is_archived_but_inflight_and_active_are_untouched(self):
        partial = make_capture(self.raw, "cap_partial", partial=True)
        active = make_capture(self.raw, "cap_active")
        inflight = self.raw / "inflight/cap_writing"
        inflight.mkdir(parents=True)
        (inflight / "response.body").write_bytes(b"prefix")
        result = run_archive(self.raw, self.archives, delete_source=True, excluded_ids=["cap_active"])
        self.assertEqual(result["archived_captures"], 1)
        self.assertFalse(partial.exists())
        self.assertTrue(active.exists())
        self.assertEqual((inflight / "response.body").read_bytes(), b"prefix")
        restore_archives(self.archive_files(), self.base / "restored")
        state = json.loads((self.base / "restored/completed/cap_partial/state.json").read_text())
        self.assertEqual(state["state"], "partial")

    def test_min_age_and_batch_limit(self):
        for number in range(5):
            make_capture(self.raw, f"cap_{number}")
        self.assertEqual(run_archive(self.raw, self.archives, min_age_seconds=3600)["archived_captures"], 0)
        self.assertEqual(run_archive(self.raw, self.archives, batch_size=2)["archived_captures"], 2)
        self.assertEqual(run_archive(self.raw, self.archives, batch_size=2)["archived_captures"], 2)
        self.assertEqual(run_archive(self.raw, self.archives, batch_size=2)["archived_captures"], 1)
        self.assertEqual(run_archive(self.raw, self.archives, batch_size=2, delete_source=True)["removed_captures"], 2)
        self.assertEqual(len(list((self.raw / "completed").iterdir())), 3)
        restore_archives(self.archive_files(), self.base / "restored")
        self.assertEqual(len(list((self.base / "restored/completed").iterdir())), 5)

    def test_symlinks_are_not_archived(self):
        capture = make_capture(self.raw)
        (capture / "linked").symlink_to(capture / "response.body")
        with self.assertRaises(ValueError):
            run_archive(self.raw, self.archives, delete_source=True)
        self.assertTrue(capture.exists())

    def test_unsafe_missing_duplicate_and_bad_hash_members_are_rejected(self):
        for broken in ("traversal", "link", "duplicate", "missing", "hash"):
            with self.subTest(broken=broken):
                path = self.base / f"{broken}.tar.gz"
                name = "completed/cap_one/file"
                digest = "0" * 64 if broken == "hash" else hashlib.sha256(b"x").hexdigest()
                manifest = {"schema_version": 1, "files": {name: {"size": 1, "sha256": digest}}}
                with tarfile.open(path, "w:gz") as archive:
                    payload = json.dumps(manifest).encode()
                    info = tarfile.TarInfo("manifest.json")
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
                    if broken != "missing":
                        info = tarfile.TarInfo("../outside" if broken == "traversal" else name)
                        info.size = 1
                        if broken == "link":
                            info.type = tarfile.SYMTYPE
                            info.linkname = "../../outside"
                        archive.addfile(info, io.BytesIO(b"x"))
                        if broken == "duplicate":
                            archive.addfile(info, io.BytesIO(b"x"))
                with self.assertRaises(ValueError):
                    restore_archives([path], self.base / f"restored_{broken}")
                self.assertFalse((self.base / "outside").exists())

    def test_restore_refuses_existing_output(self):
        make_capture(self.raw)
        run_archive(self.raw, self.archives)
        restore_archives(self.archive_files(), self.base / "restored")
        with self.assertRaises(FileExistsError):
            restore_archives(self.archive_files(), self.base / "restored")

    def test_configuration_env_and_cli_override(self):
        with patch.dict(os.environ, {"CAPTURE_ARCHIVE_DIR": str(self.archives),
            "CAPTURE_ARCHIVE_INTERVAL_SECONDS": "12", "CAPTURE_ARCHIVE_MIN_AGE_SECONDS": "34",
            "CAPTURE_ARCHIVE_BATCH_SIZE": "56", "CAPTURE_ARCHIVE_DELETE_SOURCE": "true"}):
            settings = parse_args(["--upstream-url", "http://upstream", "--no-archive-delete-source"])
        self.assertEqual(settings.archive_dir, self.archives)
        self.assertEqual((settings.archive_interval_seconds, settings.archive_min_age_seconds, settings.archive_batch_size), (12, 34, 56))
        self.assertFalse(settings.archive_delete_source)

    def test_configuration_rejects_invalid_and_nested_paths(self):
        for kwargs in ({"archive_interval_seconds": 0}, {"archive_interval_seconds": float("nan")},
                       {"archive_min_age_seconds": -1}, {"archive_batch_size": 0},
                       {"archive_dir": self.raw / "archives"}, {"archive_delete_source": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Settings("127.0.0.1", 0, "http://upstream", self.raw.parent, 30, **kwargs)


class ArchiveProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_scheduler_archives_startup_backlog_and_later_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = make_capture(root / "capture/raw")
            app = create_app(Settings("127.0.0.1", 0, "http://upstream", root / "capture", 30,
                archive_dir=root / "archives", archive_interval_seconds=0.02,
                archive_min_age_seconds=0, archive_delete_source=True),
                httpx.MockTransport(lambda _: httpx.Response(200, stream=httpx.ByteStream(b"ok"))))
            async with app.router.lifespan_context(app):
                for _ in range(100):
                    if not first.exists(): break
                    await asyncio.sleep(0.01)
                self.assertFalse(first.exists())
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
                    self.assertEqual((await client.post("/test", content=b"hello")).content, b"ok")
                    for _ in range(100):
                        if app.state.archive_manager.metrics()["archived_captures"] == 2: break
                        await asyncio.sleep(0.01)
                    health = (await client.get("/healthz")).json()
                    self.assertEqual(health["archive"]["archived_captures"], 2)
                    self.assertEqual(health["archive"]["removed_captures"], 2)
                    self.assertEqual(health["archive"]["failures"], 0)
            self.assertEqual(restore_archives(list((root / "archives").rglob("*.tar.gz")), root / "restore"), 2)

    async def test_slow_archiving_does_not_block_proxy_or_health(self):
        started, release = threading.Event(), threading.Event()
        def slow_archive(*args, **kwargs):
            started.set()
            release.wait(5)
            return {"archived_captures": 0, "removed_captures": 0, "archive_bytes": 0}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(Settings("127.0.0.1", 0, "http://upstream", root / "capture", 30,
                archive_dir=root / "archives"), httpx.MockTransport(lambda _: httpx.Response(200, stream=httpx.ByteStream(b"ok"))))
            with patch("capture_archive.archive_completed", slow_archive):
                async with app.router.lifespan_context(app):
                    try:
                        for _ in range(100):
                            if started.is_set(): break
                            await asyncio.sleep(0.01)
                        self.assertTrue(started.is_set())
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
                            response = await asyncio.wait_for(client.post("/test", content=b"hello"), 1)
                            self.assertEqual(response.content, b"ok")
                            health = await asyncio.wait_for(client.get("/healthz"), 1)
                            self.assertTrue(health.json()["archive"]["running"])
                        await app.state.archive_manager.close(timeout=0.01)
                        self.assertEqual(app.state.archive_manager.metrics()["last_failure"]["stage"], "shutdown_timeout")
                    finally:
                        release.set()

    async def test_archive_failure_and_periodic_retry_do_not_break_forwarding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(Settings("127.0.0.1", 0, "http://upstream", root / "capture", 30,
                archive_dir=root / "archives", archive_interval_seconds=0.02),
                httpx.MockTransport(lambda _: httpx.Response(201, stream=httpx.ByteStream(b"unchanged"))))
            with patch("capture_archive.archive_completed", side_effect=OSError("private-token-message")):
                async with app.router.lifespan_context(app):
                    for _ in range(100):
                        if app.state.archive_manager.runs >= 2: break
                        await asyncio.sleep(0.01)
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
                        response = await client.post("/test", content=b"body")
                        self.assertEqual((response.status_code, response.content), (201, b"unchanged"))
                        health = await client.get("/healthz")
                        self.assertGreaterEqual(health.json()["archive"]["failures"], 2)
                        self.assertNotIn("private-token-message", health.text)
                    self.assertGreaterEqual(app.state.archive_manager.runs, 2)

    async def test_default_is_disabled_and_creates_no_archive_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(Settings("127.0.0.1", 0, "http://upstream", root, 30))
            async with app.router.lifespan_context(app):
                self.assertFalse(app.state.archive_manager.metrics()["enabled"])
                self.assertIsNone(app.state.archive_manager.task)
                self.assertFalse((root / "index.sqlite3").exists())
