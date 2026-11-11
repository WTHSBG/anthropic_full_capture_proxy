"""新版 CC 的 compact 关联与 system 运行时元数据兼容，保留原有导出策略。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from api_protocols import (ANTHROPIC, CHAT, clean_cc_system_text, infer_protocol,
                           normalize_cc_request, normalize_round)
from capture_core import write_json
from export_sharegpt import export_sharegpt
from export_sharegpt_recovered import export_sharegpt_recovered
from export_sharegpt_recovered_merged import export_sharegpt_recovered_merged
from finalize import finalize_dataset
import test_core as core_fixtures


EXPORTERS = (export_sharegpt, export_sharegpt_recovered, export_sharegpt_recovered_merged)


class CCCompactCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        helper = core_fixtures.FinalizerTests()
        summary = "Exact compact summary with native session proof. " * 8
        prompt = ("CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n"
                  "Your task is to create a detailed summary of the conversation so far.\n<summary>")
        self.captures, self.harbor = self.root / "captures", self.root / "harbor"
        helper._write_unindexed_capture(self.captures, "cap_compact", "msg_compact",
            session_id="session-compact", prompt=prompt, response_text="<summary>" + summary + "</summary>")
        self.session = helper._write_compact_boundary_session(self.harbor,
            session_id="session-compact", summary=summary)
        self.request_path = self.captures / "raw/completed/cap_compact/request.json"

    def append_system(self, text):
        request = json.loads(self.request_path.read_text())
        request["body_json"]["messages"].append({"role": "system", "content": text})
        write_json(self.request_path, request)
        self.request_path.with_name("request.body").write_text(json.dumps(request["body_json"]))

    def native(self, *, anchor=False, duplicate=False, conflicting_parent=False, wrong_session=False):
        records = [json.loads(line) for line in self.session.read_text().splitlines()]
        if anchor:
            records[0]["compactMetadata"] = {"preservedSegment": {"anchorUuid": "synthetic-1"}}
            records[1]["parentUuid"] = "intermediate-attachment"
        if duplicate or conflicting_parent:
            second = copy.deepcopy(records[0])
            second["uuid"] = "boundary-2"
            if conflicting_parent:
                second.pop("compactMetadata", None)
                records[1]["parentUuid"] = "boundary-2"
            records.insert(1, second)
        if wrong_session:
            records[-1]["sessionId"] = "another-session"
        self.session.write_text("".join(json.dumps(record) + "\n" for record in records))

    def finalize(self, name="dataset"):
        return finalize_dataset(self.captures, self.harbor, self.root / name)

    def test_legacy_and_new_compact_shapes_recover(self):
        self.assertEqual(self.finalize("legacy")["matched"], 1)
        self.append_system("<total_tokens>123</total_tokens>")
        self.assertEqual(self.finalize("system_tail")["matched"], 1)
        self.native(anchor=True)
        result = self.finalize("anchor")
        self.assertEqual((result["matched"], result["unmatched"]), (1, 0))
        self.assertEqual(result["compact_recovery"]["recovered_associations"], 1)

    def test_unknown_trailing_system_is_not_compact_evidence(self):
        self.append_system("<total_tokens>123</total_tokens>Use a different summary policy.")
        result = self.finalize()
        self.assertEqual((result["matched"], result["unmatched"]), (0, 1))

    def test_new_compact_recovery_exports_masked_history(self):
        self.append_system("<total_tokens>123</total_tokens>")
        request = json.loads(self.request_path.read_text())
        request["body_json"]["messages"][:0] = [
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"}]
        write_json(self.request_path, request)
        self.request_path.with_name("request.body").write_text(json.dumps(request["body_json"]))
        self.native(anchor=True)
        original = {path: path.read_bytes() for path in self.captures.rglob("*") if path.is_file()}
        self.assertEqual(self.finalize()["matched"], 1)
        for exporter in (export_sharegpt_recovered, export_sharegpt_recovered_merged):
            output = self.root / exporter.__module__
            report = exporter(self.root / "dataset", output, "separate")
            records = [json.loads(line) for line in (output / "sharegpt.jsonl").read_text().splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual([m["role"] for m in records[0]["messages"]],
                             ["user", "masked_assistant", "user", "assistant"])
            self.assertEqual(report["masked_assistant_messages"], 1)
            self.assertNotIn("total_tokens", json.dumps(records))
        self.assertEqual({path: path.read_bytes() for path in original}, original)

    def test_anchor_takes_precedence_over_conflicting_parent(self):
        self.native(anchor=True, conflicting_parent=True)
        result = self.finalize()
        self.assertEqual(result["matched"], 1)
        self.assertTrue((self.root / "dataset/tasks/task_a/subagent_acompact-recovered-boundary-1").is_dir())
        self.assertFalse((self.root / "dataset/tasks/task_a/subagent_acompact-recovered-boundary-2").exists())

    def test_duplicate_anchor_is_not_guessed(self):
        self.native(anchor=True, duplicate=True)
        result = self.finalize()
        self.assertEqual((result["matched"], result["unmatched"]), (0, 1))

    def test_anchor_does_not_bypass_session_check(self):
        self.native(anchor=True, wrong_session=True)
        result = self.finalize()
        self.assertEqual((result["matched"], result["unmatched"]), (0, 1))


class CCExportCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input = self.root / "input"

    def add_round(self, number, messages, answer, *, system="base", model="test-model", protocol=None):
        directory = self.input / "tasks/task_a/main_agent" / f"round_{number:06d}"
        write_json(directory / "request.json", {"body": {"json": {
            "model": model, "system": system, "messages": messages}}, "association": {"session_id": "s"}})
        transport = {"aggregation_complete": True}
        if protocol:
            transport["api_protocol"] = protocol
        write_json(directory / "response.json", {"message": {"id": f"msg-{number}", "type": "message",
            "role": "assistant", "content": [{"type": "text", "text": answer}], "stop_reason": "end_turn"},
            "state": {"state": "complete"}, "transport": transport})

    def records(self, exporter, suffix):
        output = self.root / (exporter.__module__ + suffix)
        exporter(self.input, output, "separate")
        return [json.loads(path.read_text()) for path in sorted(output.rglob("main_agent_*.json"))]

    def test_runtime_system_metadata_exports_through_all_entrypoints(self):
        before = None
        for as_blocks in (False, True):
            for protocol in (None, ANTHROPIC):
                def stats(count):
                    text = f"<total_tokens>{count}</total_tokens>"
                    return [{"type": "text", "text": text}] if as_blocks else text
                first = [{"role": "system", "content": "leading instructions"}, {"role": "user", "content": "q"}]
                self.add_round(1, first + [{"role": "system", "content": stats(100)}], "first answer",
                               system="base\n<total_tokens>100</total_tokens>", protocol=protocol)
                self.add_round(2, first + [{"role": "assistant", "content": "first answer"},
                    {"role": "user", "content": "continue"}, {"role": "system", "content": stats(200)}], "last answer",
                    system="base\n<total_tokens>200</total_tokens>", protocol=protocol)
                before = {path: path.read_bytes() for path in self.input.rglob("*.json")}
                for exporter in EXPORTERS:
                    with self.subTest(exporter=exporter.__module__, blocks=as_blocks, protocol=protocol):
                        records = self.records(exporter, f"_{as_blocks}_{protocol}")
                        self.assertEqual(len(records), 1)
                        messages = records[0]["messages"]
                        self.assertEqual(messages[0], {"role": "system", "content": "base\nleading instructions"})
                        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user", "assistant"])
                        self.assertNotIn("total_tokens", json.dumps(records))
                        self.assertEqual(messages[2]["content"], "first answer")
                        self.assertEqual(messages[-1]["content"], "last answer")
                self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_semantic_middle_system_is_never_moved_to_start(self):
        self.add_round(1, [{"role": "user", "content": "q"}, {"role": "system",
            "content": "<total_tokens>10</total_tokens>New instructions"}], "answer")
        for exporter in EXPORTERS:
            with self.subTest(exporter=exporter.__module__):
                try:
                    records = self.records(exporter, "_semantic")
                except ValueError as error:
                    # 当前默认导出器拒绝该角色；扩展导出器若支持，也须保留时序。
                    self.assertIn("system", str(error))
                else:
                    messages = records[0]["messages"]
                    self.assertEqual([m["role"] for m in messages], ["system", "user", "system", "assistant"])
                    self.assertEqual(messages[2]["content"], "New instructions")

    def test_discontinuous_history_and_model_change_still_split(self):
        self.add_round(1, [{"role": "user", "content": "first question"}], "EARLY_ANSWER")
        self.add_round(2, [{"role": "user", "content": "another question"}], "last answer", model="another-model")
        for exporter in EXPORTERS:
            with self.subTest(exporter=exporter.__module__):
                records = self.records(exporter, "_split")
                self.assertEqual(len(records), 2)
                self.assertIn("EARLY_ANSWER", json.dumps(records))

    def test_user_assistant_tokens_and_plain_system_whitespace_are_preserved(self):
        self.add_round(1, [{"role": "user", "content": "literal <total_tokens>1</total_tokens>"}],
                       "literal <total_tokens>2</total_tokens>", system="  original instructions\n")
        for exporter in EXPORTERS:
            with self.subTest(exporter=exporter.__module__):
                record = self.records(exporter, "_literal")[0]
                self.assertEqual(record["messages"][0]["content"], "  original instructions\n")
                self.assertIn("<total_tokens>1</total_tokens>", record["messages"][1]["content"])
                self.assertIn("<total_tokens>2</total_tokens>", record["messages"][-1]["content"])

    def test_anthropic_response_identifies_legacy_requests_with_system_tail(self):
        request = {"messages": [{"role": "user", "content": "q"},
                               {"role": "system", "content": "<total_tokens>1</total_tokens>"}]}
        response = {"role": "assistant", "content": [{"type": "text", "text": "answer"}]}
        before = copy.deepcopy(request)
        self.assertEqual(infer_protocol(request, response), ANTHROPIC)
        normalized, _ = normalize_round(request, response, {})
        self.assertEqual(normalized["messages"], [{"role": "user", "content": "q"}])
        self.assertEqual(request, before)

    def test_chat_response_keeps_its_existing_protocol_and_semantic_checks(self):
        request = {"messages": [{"role": "user", "content": "q"}, {"role": "system", "content": "new policy"}]}
        response = {"choices": [{"message": {"role": "assistant", "content": "answer"}, "finish_reason": "stop"}]}
        self.assertEqual(infer_protocol(request, response), CHAT)
        with self.assertRaisesRegex(ValueError, "system"):
            normalize_round(request, response, {})

    def test_system_cleaning_preserves_non_runtime_instructions(self):
        request = {"system": [{"type": "text", "text": "x-anthropic-billing-header: cch=1"},
                              {"type": "text", "text": "base <total_tokens>1</total_tokens>", "cache_control": {"type": "ephemeral"}}],
                   "messages": [{"role": "user", "content": "q"}]}
        before = copy.deepcopy(request)
        normalized = normalize_cc_request(request)
        self.assertEqual(normalized["system"], [{"type": "text", "text": "base", "cache_control": {"type": "ephemeral"}}])
        self.assertEqual(request, before)
        self.assertEqual(clean_cc_system_text("  ordinary instructions\n"), "  ordinary instructions\n")
