"""已收尾 capture 的定时无损归档及离线恢复。只使用 Python 标准库。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path, PurePosixPath

from capture_core import utc_timestamp


def digest_file(path: Path) -> str:
    with path.open("rb") as handle:
        return digest_stream(handle)


def digest_stream(handle) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def member_name(name: str) -> str:
    parts = PurePosixPath(name).parts
    if (len(parts) != 3 or parts[0] != "completed"
            or any(p in {".", ".."} for p in parts)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", parts[1])
            or "\\" in name or str(PurePosixPath(name)) != name):
        raise ValueError("归档成员路径无效")
    return parts[1]


def snapshot(directory: Path) -> dict:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("capture 必须是普通目录")
    files = {}
    for path in sorted(directory.iterdir()):
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("capture 中存在非普通文件")
        name = f"completed/{directory.name}/{path.name}"
        member_name(name)
        before = path.stat()
        digest = digest_file(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError("capture 在读取期间变化")
        files[name] = {"size": after.st_size, "sha256": digest}
    if not files:
        raise ValueError("capture 为空")
    return files


def verify_archive(path: Path) -> dict:
    """逐文件读回并校验，不信任 tar 路径、链接或清单内容。"""
    with tarfile.open(path, "r:gz") as archive:
        first = archive.next()
        if first is None or first.name != "manifest.json" or not first.isfile() or first.size > 4 * 1024 * 1024:
            raise ValueError("归档缺少有效清单")
        manifest = json.load(archive.extractfile(first))
        if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), dict):
            raise ValueError("归档清单格式无效")
        expected = manifest["files"]
        if not expected:
            raise ValueError("归档清单为空")
        for name, entry in expected.items():
            member_name(name)
            if (not isinstance(entry, dict) or type(entry.get("size")) is not int
                    or entry["size"] < 0
                    or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", "")))):
                raise ValueError("文件哈希清单无效")
        seen = set()
        for member in archive:
            # tarfile 的迭代器会再次返回已读取的首成员。
            if member is first:
                continue
            member_name(member.name)
            if not member.isfile() or member.name in seen or member.name not in expected:
                raise ValueError("归档包含额外成员、重复成员或链接")
            entry = expected[member.name]
            if member.size != entry["size"]:
                raise ValueError("归档文件大小不匹配")
            with archive.extractfile(member) as handle:
                digest = digest_stream(handle)
            if digest != entry["sha256"]:
                raise ValueError("归档文件哈希不匹配")
            seen.add(member.name)
        if seen != set(expected):
            raise ValueError("归档缺失文件")
        return manifest


def write_archive(root: Path, destination: Path, captures: list[Path]) -> dict:
    files = {}
    for directory in captures:
        files.update(snapshot(directory))
    manifest = {"schema_version": 1, "created_at": utc_timestamp(),
                "source_root": str(root.resolve()), "files": files}
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        with tarfile.open(temporary, "w:gz", compresslevel=6) as archive:
            payload = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(payload)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(payload))
            for name in files:
                archive.add(root / name, arcname=name, recursive=False)
        if verify_archive(temporary) != manifest:
            raise ValueError("归档清单读回不一致")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        # 目录项也落盘，然后才能写索引和清理源文件。
        if os.name == "posix":
            fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return manifest
    finally:
        temporary.unlink(missing_ok=True)


def remove_verified(directory: Path, manifest: dict) -> None:
    expected = {name: entry for name, entry in manifest["files"].items()
                if member_name(name) == directory.name}
    if snapshot(directory) != expected:
        raise ValueError("源 capture 与归档不一致，保留源文件")
    shutil.rmtree(directory)


def archive_completed(root: Path, archive_dir: Path, *, min_age_seconds: float,
                      batch_size: int, delete_source: bool, excluded_ids=()) -> dict:
    """每次最多归档一个批次。索引持久化后才允许删除；失败保留源数据。"""
    result = {"archived_captures": 0, "removed_captures": 0, "archive_bytes": 0}
    if not (root / "completed").exists():
        return result
    archive_dir.mkdir(parents=True, exist_ok=True)
    source = str(root.resolve())
    excluded = set(excluded_ids)
    with closing(sqlite3.connect(archive_dir / "index.sqlite3", timeout=0)) as index, index:
        index.execute("CREATE TABLE IF NOT EXISTS archives (archive TEXT PRIMARY KEY, manifest TEXT)")
        index.execute("CREATE TABLE IF NOT EXISTS captures (source TEXT, capture TEXT, archive TEXT, PRIMARY KEY(source, capture))")
        # 同一归档目录的调度器串行处理，避免重复创建批次。
        index.execute("BEGIN IMMEDIATE")
        candidates = []
        verified = {}
        for directory in (root / "completed").iterdir():
            if directory.name in excluded:
                continue
            if directory.is_symlink() or not directory.is_dir():
                continue
            state = json.loads((directory / "state.json").read_text())
            if state.get("state") not in {"complete", "partial"}:
                continue
            ended = datetime.fromisoformat(state["updated_at"].replace("Z", "+00:00")).timestamp()
            if time.time() - ended < min_age_seconds:
                continue
            old = index.execute("SELECT c.archive, a.manifest FROM captures c JOIN archives a ON c.archive=a.archive WHERE c.source=? AND c.capture=?",
                                (source, directory.name)).fetchone()
            if old:
                old_path = archive_dir / old[0]
                if old_path.is_file():
                    if delete_source:
                        if old[0] not in verified:
                            verified[old[0]] = verify_archive(old_path)
                        if verified[old[0]] != json.loads(old[1]):
                            raise ValueError("归档与持久化索引不一致")
                        remove_verified(directory, verified[old[0]])
                        result["removed_captures"] += 1
                        if result["removed_captures"] >= batch_size:
                            break
                    continue
            candidates.append(directory)
            if len(candidates) + result["removed_captures"] >= batch_size:
                break
        if not candidates:
            return result
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        folder = archive_dir / day
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"captures-{uuid.uuid4().hex}.tar.gz"
        manifest = write_archive(root, destination, candidates)
        relative = str(destination.relative_to(archive_dir))
        encoded = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
        index.execute("INSERT INTO archives VALUES (?, ?)", (relative, encoded))
        index.executemany("INSERT OR REPLACE INTO captures VALUES (?, ?, ?)",
                          [(source, p.name, relative) for p in candidates])
        index.commit()
        result["archived_captures"] = len(candidates)
        result["archive_bytes"] = destination.stat().st_size
        if delete_source:
            # 接续持有索引锁；提交已完成，即便删除中断下次也能校验后重试。
            index.execute("BEGIN IMMEDIATE")
            for directory in candidates:
                remove_verified(directory, manifest)
                result["removed_captures"] += 1
    return result


class ArchiveManager:
    def __init__(self, root, archive_dir, *, interval_seconds, min_age_seconds,
                 batch_size, delete_source, active_ids):
        self.root = root
        self.archive_dir = archive_dir
        self.interval_seconds = interval_seconds
        self.min_age_seconds = min_age_seconds
        self.batch_size = batch_size
        self.delete_source = delete_source
        self.active_ids = active_ids
        self.stop = asyncio.Event()
        self.task = None
        self.running = False
        self.runs = self.failures = 0
        self.totals = {"archived_captures": 0, "removed_captures": 0, "archive_bytes": 0}
        self.last_run = self.last_failure = None

    def start(self):
        if self.archive_dir is not None:
            self.task = asyncio.create_task(self.run())

    def failure(self, stage, error):
        self.failures += 1
        self.last_failure = {"stage": stage, "error_type": type(error).__name__, "at": utc_timestamp()}

    async def run(self):
        while not self.stop.is_set():
            self.running = True
            try:
                result = await asyncio.to_thread(archive_completed, self.root, self.archive_dir,
                    min_age_seconds=self.min_age_seconds, batch_size=self.batch_size,
                    delete_source=self.delete_source, excluded_ids=self.active_ids())
                for key, value in result.items():
                    self.totals[key] += value
            except Exception as exc:
                self.failure("archive", exc)
            finally:
                self.runs += 1
                self.last_run = utc_timestamp()
                self.running = False
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                pass

    def metrics(self):
        return {"enabled": self.archive_dir is not None,
                "status": "degraded" if self.failures else "ok", "running": self.running,
                "directory": str(self.archive_dir) if self.archive_dir is not None else None,
                "interval_seconds": self.interval_seconds, "min_age_seconds": self.min_age_seconds,
                "batch_size": self.batch_size, "delete_source": self.delete_source,
                "runs": self.runs, **self.totals, "failures": self.failures,
                "last_run": self.last_run, "last_failure": self.last_failure}

    async def close(self, timeout=5):
        self.stop.set()
        if self.task is not None:
            _, pending = await asyncio.wait([self.task], timeout=timeout)
            if pending:
                # 不取消线程中的压缩/校验，避免误把尚未结束的操作当成完成。
                self.failure("shutdown_timeout", TimeoutError())


def restore_archives(archives: list[Path], output: Path) -> int:
    """校验后恢复 completed/；拒绝覆盖已有 capture 或提取不安全 tar 成员。"""
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise FileExistsError("恢复输出目录须不存在或为空")
    manifests = [verify_archive(path) for path in archives]
    ids = set()
    for manifest in manifests:
        batch = {member_name(name) for name in manifest["files"]}
        if ids & batch or any((output / "completed" / value).exists() for value in batch):
            raise FileExistsError("恢复 capture 已存在；请使用新的输出目录")
        ids.update(batch)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".capture-restore-", dir=output.parent) as temporary:
        stage = Path(temporary)
        for path, manifest in zip(archives, manifests):
            with tarfile.open(path, "r:gz") as archive:
                seen = set()
                for member in archive:
                    if member.name == "manifest.json":
                        continue
                    member_name(member.name)
                    if not member.isfile() or member.name in seen or member.name not in manifest["files"]:
                        raise ValueError("恢复时归档成员发生变化")
                    seen.add(member.name)
                    destination = stage / member.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with destination.open("xb") as handle:
                        shutil.copyfileobj(archive.extractfile(member), handle)
                    entry = manifest["files"][member.name]
                    if destination.stat().st_size != entry["size"] or digest_file(destination) != entry["sha256"]:
                        raise ValueError("恢复时哈希不匹配")
                if seen != set(manifest["files"]):
                    raise ValueError("恢复时归档缺失文件")
        (output / "completed").mkdir(parents=True, exist_ok=True)
        for value in ids:
            destination = output / "completed" / value
            if destination.exists():
                raise FileExistsError("恢复目录出现同名 capture")
            os.rename(stage / "completed" / value, destination)
    return len(ids)


def main():
    parser = argparse.ArgumentParser(description="校验并恢复自动归档的原始 capture")
    parser.add_argument("--archives", type=Path, nargs="+", required=True)
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="恢复后的 raw 根目录；须不存在或为空，不覆盖已有 capture")
    args = parser.parse_args()
    print(json.dumps({"restored_captures": restore_archives(args.archives, args.output_dir)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
