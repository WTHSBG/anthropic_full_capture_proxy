from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from capture_core import write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "finalize-node-split.py"
SPEC = importlib.util.spec_from_file_location("finalize_node_split", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
finalize_node_split = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(finalize_node_split)

TEST_TMP_ROOT = PROJECT_ROOT / ".test_tmp"
TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)


def write_capture(root: Path, capture_id: str, message_id: str) -> None:
    directory = root / "raw" / "completed" / capture_id
    directory.mkdir(parents=True)
    request_body = json.dumps(
        {
            "model": "test-model",
            "messages": [{"role": "user", "content": message_id}],
            "max_tokens": 32,
        }
    ).encode()
    response_body = json.dumps(
        {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
        }
    ).encode()
    (directory / "request.body").write_bytes(request_body)
    (directory / "response.body").write_bytes(response_body)
    write_json(
        directory / "request.json",
        {
            "capture_id": capture_id,
            "captured_at": "2026-01-01T00:00:00Z",
            "method": "POST",
            "path": "/v1/messages",
            "url": "http://proxy/v1/messages",
            "is_messages_request": True,
        },
    )
    write_json(
        directory / "response.json",
        {
            "capture_id": capture_id,
            "status_code": 200,
            "content_type": "application/json",
            "stream": False,
            "message_id": message_id,
            "message": json.loads(response_body),
        },
    )
    write_json(directory / "state.json", {"state": "complete"})


def write_session(
    node_root: Path,
    task_dir_name: str,
    session_id: str,
    message_id: str,
    event_uuid: str,
) -> Path:
    project = (
        node_root
        / "worker0"
        / "logs"
        / task_dir_name
        / "logs"
        / "projects"
        / "-workspace"
    )
    project.mkdir(parents=True)
    message = {
        "id": message_id,
        "content": [{"type": "text", "text": "answer"}],
    }
    record = {
        "type": "assistant",
        "sessionId": session_id,
        "uuid": event_uuid,
        "timestamp": "2026-01-01T00:00:01Z",
        "message": message,
    }
    session_path = project / f"{session_id}.jsonl"
    session_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return session_path


class NodeSplitFinalizerTests(unittest.TestCase):
    def test_cli_splits_tasks_keeps_full_fields_and_uses_acompact_fix(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT) as temporary:
            root = Path(temporary)
            capture_root = root / "captures"
            node_root = root / "node0"
            output_root = root / "dataset"

            write_capture(capture_root, "cap_success", "msg_success")
            write_capture(capture_root, "cap_failed", "msg_failed")
            success_session = write_session(
                node_root,
                "arvo_1-successid",
                "session-success",
                "msg_success",
                "event-success",
            )
            write_session(
                node_root,
                "arvo_2-failedid",
                "session-failed",
                "msg_failed",
                "event-failed",
            )

            # 同一事件同时出现在 main 与 agent-acompact，必须由 22658e7 的
            # 共享 finalize.py 逻辑去重，而不是产生 conflict 或重复轨迹。
            main_record = json.loads(success_session.read_text(encoding="utf-8"))
            acompact = (
                success_session.parent
                / "session-success"
                / "subagents"
                / "agent-acompact-auto.jsonl"
            )
            acompact.parent.mkdir(parents=True)
            acompact_record = {
                **main_record,
                "agentId": "acompact-auto",
                "isSidechain": True,
            }
            acompact.write_text(json.dumps(acompact_record) + "\n", encoding="utf-8")

            result_root = node_root / "worker0" / "result"
            result_root.mkdir(parents=True)
            (result_root / "arvo_1_successid.log").write_text(
                '{"vul_exit_code": 139, "fix_exit_code": 0}\n',
                encoding="utf-8",
            )
            (result_root / "arvo_2_failedid.log").write_text(
                '{"vul_exit_code": 0, "fix_exit_code": 0}\n',
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "--capture-dir",
                    str(capture_root),
                    "--harbor-run-dir",
                    str(node_root),
                    "--output-dir",
                    str(output_root),
                ],
                cwd=PROJECT_ROOT,
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertIn('"successful": 1', completed.stdout)
            self.assertIn('"failed": 1', completed.stdout)

            success_round = (
                output_root
                / "successful"
                / "tasks"
                / "arvo_1-successid"
                / "main_agent"
                / "round_000001"
            )
            failed_round = (
                output_root
                / "failed"
                / "tasks"
                / "arvo_2-failedid"
                / "main_agent"
                / "round_000001"
            )
            self.assertTrue(success_round.is_dir())
            self.assertTrue(failed_round.is_dir())

            request = json.loads(
                (success_round / "request.json").read_text(encoding="utf-8")
            )
            response = json.loads(
                (success_round / "response.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                list(request),
                finalize_node_split.FINAL_REQUEST_PARTS,
            )
            self.assertEqual(
                list(response),
                finalize_node_split.FINAL_RESPONSE_PARTS,
            )

            report = json.loads(
                (output_root / "finalization_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                report["exported_trajectory_tasks"],
                {"successful": 1, "failed": 1},
            )
            self.assertEqual(
                report["result_log_classifications"],
                {"correct": 1, "vul_not_crashed": 1},
            )
            self.assertEqual(report["matched"], 2)
            self.assertEqual(report["conflicts"], 0)
            self.assertEqual(
                report["session_scan"]["acompact_mirror_message_ids"], 1
            )


if __name__ == "__main__":
    unittest.main()
