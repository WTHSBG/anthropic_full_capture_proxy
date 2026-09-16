from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import export_sharegpt_arvo
from capture_core import write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_TMP_ROOT = PROJECT_ROOT / ".test_tmp"
TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)


class ArvoShareGPTExporterTests(unittest.TestCase):
    def test_export_matches_arvo_sample_structure_and_pretty_format(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT) as temporary:
            root = Path(temporary)
            input_dir = root / "dataset"
            output_dir = root / "sharegpt"
            round_dir = (
                input_dir
                / "tasks"
                / "user_cybergym_arvo_50305"
                / "main_agent"
                / "round_000001"
            )
            round_dir.mkdir(parents=True)

            request_body = {
                "model": "test-model",
                "system": "system prompt",
                "messages": [
                    {"role": "user", "content": "solve the task"},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "thinking",
                                "thinking": "run a command",
                                "signature": "ignored-signature",
                            },
                            {
                                "type": "tool_use",
                                "id": "call_1",
                                "name": "shell",
                                "input": {"command": "pwd"},
                            },
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "call_1",
                                "content": "/workspace",
                                "is_error": False,
                            }
                        ],
                    },
                ],
                "tools": [
                    {
                        "name": "shell",
                        "description": "Run a command",
                        "input_schema": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                        },
                    }
                ],
            }
            response_message = {
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "the command succeeded",
                        "signature": "ignored-signature-2",
                    },
                    {"type": "text", "text": "done"},
                ]
            }
            write_json(round_dir / "request.json", {"body": {"json": request_body}})
            write_json(
                round_dir / "response.json",
                {
                    "message": response_message,
                    "state": {"state": "complete"},
                    "transport": {"aggregation_complete": True},
                },
            )

            report = export_sharegpt_arvo.export_sharegpt(input_dir, output_dir)
            output_path = (
                output_dir / "user_cybergym_arvo_50305" / "main_agent_1.json"
            )
            output_text = output_path.read_text(encoding="utf-8")
            generated = json.loads(output_text)

            generated_call = next(
                message
                for message in generated["messages"]
                if message.get("tool_calls")
            )["tool_calls"][0]
            generated_tool_message = next(
                message
                for message in generated["messages"]
                if message["role"] == "tool"
            )

            self.assertEqual(list(generated), ["id", "messages", "tools"])
            self.assertEqual(
                list(generated_call),
                ["id", "type", "name", "arguments", "function"],
            )
            self.assertEqual(
                list(generated["tools"][0]),
                ["type", "name", "description", "parameters", "function"],
            )
            self.assertEqual(
                list(generated_tool_message),
                ["role", "name", "tool_call_id", "content", "is_error"],
            )
            self.assertTrue(output_text.startswith('{\n  "id":'))
            self.assertGreater(len(output_text.splitlines()), 1)
            self.assertEqual(report["reasoning_mode"], "separate")
            self.assertTrue(report["standard_structure"])
            self.assertEqual(
                report["format"], "arvo-50305-hybrid-tool-calling"
            )

    def test_export_recovers_response_from_following_request_history(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT) as temporary:
            root = Path(temporary)
            input_dir = root / "dataset"
            output_dir = root / "sharegpt"
            agent_dir = input_dir / "tasks/task_a/main_agent"
            first_round = agent_dir / "round_000001"
            second_round = agent_dir / "round_000002"

            common_request = {
                "model": "test-model",
                "system": "system prompt",
                "tools": [],
            }
            canonical_content = [{"type": "text", "text": "canonical answer"}]
            write_json(
                first_round / "request.json",
                {
                    "body": {
                        "json": {
                            **common_request,
                            "messages": [{"role": "user", "content": "start"}],
                        }
                    }
                },
            )
            write_json(
                first_round / "response.json",
                {
                    "message": {
                        "content": [{"type": "text", "text": "bad aggregation"}],
                        "stop_reason": "end_turn",
                    },
                    "state": {"state": "complete"},
                    "transport": {"aggregation_complete": True},
                },
            )
            write_json(
                second_round / "request.json",
                {
                    "body": {
                        "json": {
                            **common_request,
                            "messages": [
                                {"role": "user", "content": "start"},
                                {
                                    "role": "assistant",
                                    "content": canonical_content,
                                },
                                {"role": "user", "content": "continue"},
                            ],
                        }
                    }
                },
            )
            write_json(
                second_round / "response.json",
                {
                    "message": {
                        "content": [{"type": "text", "text": "done"}],
                        "stop_reason": "end_turn",
                    },
                    "state": {"state": "complete"},
                    "transport": {"aggregation_complete": True},
                },
            )

            report = export_sharegpt_arvo.export_sharegpt(input_dir, output_dir)
            generated = json.loads(
                (output_dir / "task_a/main_agent_1.json").read_text(
                    encoding="utf-8"
                )
            )
            recovery_report = json.loads(
                (output_dir / "recovery_report.json").read_text(encoding="utf-8")
            )

            self.assertEqual(report["sharegpt_files"], 1)
            self.assertEqual(report["context_splits"], 0)
            self.assertEqual(report["recovered_rounds"], 1)
            self.assertEqual(
                report["recovery_report"],
                str((output_dir / "recovery_report.json").resolve()),
            )
            self.assertEqual(recovery_report["recovered_round_count"], 1)
            self.assertEqual(
                recovery_report["recoveries"][0]["action"],
                "replace_aggregation_from_following_request",
            )
            self.assertEqual(
                [message["content"] for message in generated["messages"]],
                ["system prompt", "start", "canonical answer", "continue", "done"],
            )


if __name__ == "__main__":
    unittest.main()
