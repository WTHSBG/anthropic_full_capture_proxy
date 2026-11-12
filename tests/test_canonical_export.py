"""工具包(1)固定入口、CC compact 兼容和导出策略的端到端验证。"""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import export_sharegpt as base
from capture_core import write_json
from finalize import finalize_dataset
import test_core as fixtures

ROOT = Path(__file__).resolve().parents[1]


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": text}


class CanonicalExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input = self.root / "input"

    def add_round(self, number, messages, answer, *, system="base", model="test-model"):
        directory = self.input / "tasks/task_a/main_agent" / f"round_{number:06d}"
        write_json(directory / "request.json", {"association": {"session_id": "s"},
            "body": {"json": {"model": model, "system": system, "messages": messages}}})
        write_json(directory / "response.json", {"state": {"state": "complete"},
            "transport": {"aggregation_complete": True}, "message": {
                "id": f"msg-{number}", "role": "assistant", "content": [{"type": "text", "text": answer}],
                "stop_reason": "end_turn"}})

    def run_export(self, *, input_dir=None):
        output = self.root / "output"
        before = {p: p.read_bytes() for p in (input_dir or self.input).rglob("*") if p.is_file()}
        process = subprocess.run([sys.executable, str(ROOT / "run_canonical_export.py"),
            "--input-dir", str(input_dir or self.input), "--output-dir", str(output)],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        records = [json.loads(line) for line in (output / "sharegpt.jsonl").read_text().splitlines()]
        report = json.loads((self.root / "output_canonical_run_report.json").read_text())
        return records, report

    def test_imported_files_match_package_provenance(self):
        metadata = json.loads((ROOT / "EXPORT_KIT_SOURCE.json").read_text())
        for name, item in metadata["files"].items():
            with self.subTest(file=name):
                self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), item["sha256"])

    def test_fingerprints_reject_changed_dependency_and_checksum_list(self):
        target = self.root / "scripts"
        target.mkdir()
        names = ("run_canonical_export.py", "export_sharegpt.py", "export_sharegpt_recovered.py",
                 "export_sharegpt_recovered_merged.py", "capture_core.py", "SHA256SUMS")
        for name in names:
            shutil.copyfile(ROOT / name, target / name)
        def verify():
            return subprocess.run([sys.executable, str(target / "run_canonical_export.py"), "--verify-only"],
                                  capture_output=True, text=True, timeout=10)
        self.assertEqual(verify().returncode, 0)
        source = target / "export_sharegpt.py"
        original = source.read_bytes()
        source.write_bytes(original + b"\n# modified dependency\n")
        result = verify()
        self.assertEqual(result.returncode, 2)
        self.assertIn("export_sharegpt.py", result.stderr)
        source.write_bytes(original)
        (target / "SHA256SUMS").write_text("invalid checksum list\n")
        result = verify()
        self.assertEqual(result.returncode, 2)
        self.assertIn("SHA256SUMS", result.stderr)

    def test_embedded_system_stays_at_original_position(self):
        self.add_round(1, [user("q"), assistant("earlier answer"),
            {"role": "system", "content": "new policy <total_tokens>50</total_tokens>"}, user("continue")],
            "done", system="base <total_tokens>100</total_tokens>")
        records, _ = self.run_export()
        self.assertEqual(len(records), 1)
        messages = records[0]["messages"]
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "system", "user", "assistant"])
        self.assertEqual(messages[0]["content"], "base")
        self.assertEqual(messages[3]["content"], "new policy")
        self.assertEqual(messages[-1]["content"], "done")
        self.assertNotIn("total_tokens", json.dumps(records))

    def test_image_history_keeps_only_preceding_text_prefix(self):
        image = {"role": "user", "content": [{"type": "text", "text": "screenshot"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}]}
        self.add_round(1, [user("text only")], "prefix answer")
        self.add_round(2, [user("text only"), assistant("prefix answer"), image], "image answer")
        self.add_round(3, [user("text only"), assistant("prefix answer"), image,
                          assistant("image answer"), user("continue")], "after image")
        records, report = self.run_export()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["messages"][-1]["content"], "prefix answer")
        self.assertEqual(report["image_skip"]["rounds_kept_as_prefix"], 1)
        self.assertEqual(report["image_skip"]["rounds_dropped_after_image"], 2)
        self.assertNotIn("image answer", json.dumps(records))

    def test_explicit_compaction_restart_splits_windows(self):
        self.add_round(1, [user("q")], "first")
        self.add_round(2, [user("q"), assistant("first"), user("continue")], "second")
        marker = "This session is being continued from a previous conversation. Summary: earlier work"
        self.add_round(3, [user(marker)], "resumed")
        records, _ = self.run_export()
        self.assertEqual(len(records), 2)
        self.assertCountEqual([r["messages"][-1]["content"] for r in records], ["second", "resumed"])

    def test_non_compaction_history_reset_uses_last_request_as_documented(self):
        self.add_round(1, [user("first question")], "earlier answer")
        self.add_round(2, [user("another question")], "last answer", model="another-model")
        records, _ = self.run_export()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["messages"][-1]["content"], "last answer")
        self.assertNotIn("earlier answer", json.dumps(records))

    def test_new_cc_compact_finalize_and_masked_export(self):
        helper = fixtures.FinalizerTests()
        captures, harbor = self.root / "captures", self.root / "harbor"
        summary = "Retained complete compact summary with exact session evidence. " * 8
        prompt = ("CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n"
                  "Your task is to create a detailed summary of the conversation so far.\n<summary>")
        helper._write_unindexed_capture(captures, "cap_compact", "msg_compact", session_id="s",
            prompt=prompt, response_text="<summary>" + summary + "</summary>")
        request_path = captures / "raw/completed/cap_compact/request.json"
        request = json.loads(request_path.read_text())
        request["body_json"]["messages"][:0] = [user("earlier question"), assistant("earlier answer")]
        request["body_json"]["messages"].append({"role": "system", "content": "<total_tokens>100</total_tokens>"})
        write_json(request_path, request)
        request_path.with_name("request.body").write_text(json.dumps(request["body_json"]))
        session = helper._write_compact_boundary_session(harbor, session_id="s", summary=summary)
        records = [json.loads(line) for line in session.read_text().splitlines()]
        records[0]["compactMetadata"] = {"preservedSegment": {"anchorUuid": "synthetic-1"}}
        records[1]["parentUuid"] = "attachment-1"
        session.write_text("".join(json.dumps(record) + "\n" for record in records))
        before = {p: p.read_bytes() for p in captures.rglob("*") if p.is_file()}
        dataset = self.root / "dataset"
        finalization = finalize_dataset(captures, harbor, dataset)
        self.assertEqual((finalization["matched"], finalization["unmatched"]), (1, 0))
        exported, report = self.run_export(input_dir=dataset)
        # 工具包将运行时 system 原位保留，即便清理后内容为空。
        self.assertEqual([m["role"] for m in exported[0]["messages"]],
                         ["user", "masked_assistant", "user", "system", "assistant"])
        self.assertEqual(exported[0]["messages"][-2]["content"], "")
        self.assertEqual(report["masked_assistant_messages"], 1)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
