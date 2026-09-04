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
MODULE_PATH = PROJECT_ROOT / "finalize-proc-split.py"
SPEC = importlib.util.spec_from_file_location("finalize_proc_split", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
finalize_proc_split = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = finalize_proc_split
SPEC.loader.exec_module(finalize_proc_split)

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


def write_task(
    proc_root: Path,
    group: str,
    task_id: str,
    message_id: str,
    scores: list[float],
) -> Path:
    task_dir = proc_root / group / task_id
    project = task_dir / "logs" / "projects" / "-workspace"
    project.mkdir(parents=True)
    session_id = f"session-{task_id}"
    record = {
        "type": "assistant",
        "sessionId": session_id,
        "uuid": f"event-{task_id}",
        "timestamp": "2026-01-01T00:00:01Z",
        "message": {
            "id": message_id,
            "content": [{"type": "text", "text": "answer"}],
        },
    }
    (project / f"{session_id}.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    write_json(
        task_dir / "result.json",
        {
            "task_id": task_id,
            "checks": [
                {"name": f"check-{index}", "score": score}
                for index, score in enumerate(scores)
            ],
            "error": None,
        },
    )
    return task_dir


class ProcSplitFinalizerTests(unittest.TestCase):
    def test_cli_splits_complete_tasks_and_skips_incomplete_directories(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT) as temporary:
            root = Path(temporary)
            capture_root = root / "captures"
            proc_root = root / "proc_7"
            output_root = root / "dataset"

            write_capture(capture_root, "cap-success", "msg-success")
            write_capture(capture_root, "cap-failed", "msg-failed")
            write_task(
                proc_root,
                "kernel",
                "kernel_kernelctf_CVE-2023-3776_lts",
                "msg-success",
                [1.0],
            )
            write_task(
                proc_root,
                "user",
                "user_cybergym_arvo_123",
                "msg-failed",
                [1.0, 0.0],
            )
            incomplete = proc_root / "user" / "user_cybergym_arvo_incomplete"
            (incomplete / "logs").mkdir(parents=True)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(MODULE_PATH),
                    "--capture-dir",
                    str(capture_root),
                    "--proc-dir",
                    str(proc_root),
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
                / "kernel_kernelctf_CVE-2023-3776_lts"
                / "main_agent"
                / "round_000001"
            )
            failed_round = (
                output_root
                / "failed"
                / "tasks"
                / "user_cybergym_arvo_123"
                / "main_agent"
                / "round_000001"
            )
            self.assertTrue(success_round.is_dir())
            self.assertTrue(failed_round.is_dir())
            self.assertFalse(
                (
                    output_root
                    / "failed"
                    / "tasks"
                    / "user_cybergym_arvo_incomplete"
                ).exists()
            )

            request = json.loads(
                (success_round / "request.json").read_text(encoding="utf-8")
            )
            response = json.loads(
                (success_round / "response.json").read_text(encoding="utf-8")
            )
            self.assertEqual(list(request), finalize_proc_split.FINAL_REQUEST_PARTS)
            self.assertEqual(list(response), finalize_proc_split.FINAL_RESPONSE_PARTS)

            report = json.loads(
                (output_root / "finalization_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["complete_task_directories"], 2)
            self.assertEqual(report["skipped_incomplete_task_directories"], 1)
            self.assertEqual(
                report["skipped_incomplete_problem_counts"],
                {"missing_result_json": 1, "missing_session_jsonl": 1},
            )
            self.assertEqual(
                report["result_score_classifications"],
                {"correct": 1, "incorrect": 1},
            )
            self.assertEqual(
                report["exported_trajectory_tasks"],
                {"successful": 1, "failed": 1},
            )

    def test_invalid_score_is_not_a_complete_task(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT) as temporary:
            proc_root = Path(temporary) / "proc_0"
            task_dir = write_task(
                proc_root,
                "kernel",
                "invalid-score",
                "msg-invalid",
                [0.5],
            )

            tasks, skipped, discovered = finalize_proc_split.discover_proc_tasks(
                proc_root
            )

            self.assertEqual(discovered, 1)
            self.assertEqual(tasks, [])
            self.assertEqual(skipped[0]["task_id"], task_dir.name)
            self.assertEqual(skipped[0]["problems"], ["invalid_result_json"])


if __name__ == "__main__":
    unittest.main()
