from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import export_sharegpt_recovered
from capture_core import write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_TMP_ROOT = PROJECT_ROOT / ".test_tmp"
TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)


class RecoveredShareGPTMaskingTests(unittest.TestCase):
    def _write_agent(self, input_dir: Path, agent_name: str) -> None:
        round_dir = input_dir / "tasks" / "task_a" / agent_name / "round_000001"
        request_body = {
            "model": "test-model",
            "system": "system prompt",
            "messages": [
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "second question"},
                {"role": "assistant", "content": "second answer"},
                {"role": "user", "content": "make a summary"},
            ],
            "tools": [],
        }
        response = {
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "final summary"}],
                "stop_reason": "end_turn",
            },
            "state": {"state": "complete"},
            "transport": {"aggregation_complete": True},
        }
        write_json(round_dir / "request.json", {"body": {"json": request_body}})
        write_json(round_dir / "response.json", response)

    def _export_roles(
        self, agent_name: str, *, mask_compact_assistant_history: bool
    ) -> tuple[list[str], dict]:
        with tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT) as temporary:
            root = Path(temporary)
            input_dir = root / "dataset"
            output_dir = root / "sharegpt"
            self._write_agent(input_dir, agent_name)
            report = export_sharegpt_recovered.export_sharegpt_recovered(
                input_dir,
                output_dir,
                reasoning_mode="separate",
                mask_compact_assistant_history=mask_compact_assistant_history,
            )
            record = json.loads(
                (output_dir / "task_a" / f"{agent_name}_1.json").read_text(
                    encoding="utf-8"
                )
            )
            return [message["role"] for message in record["messages"]], report

    def test_default_keeps_compact_assistant_roles(self):
        roles, report = self._export_roles(
            "subagent_acompact-auto", mask_compact_assistant_history=False
        )

        self.assertEqual(
            roles,
            ["system", "user", "assistant", "user", "assistant", "user", "assistant"],
        )
        self.assertFalse(report["mask_compact_assistant_history"])
        self.assertEqual(report["masked_assistant_messages"], 0)

    def test_masks_normal_and_recovered_compact_agents(self):
        expected_roles = [
            "system",
            "user",
            "masked_assistant",
            "user",
            "masked_assistant",
            "user",
            "assistant",
        ]
        for agent_name in (
            "subagent_acompact-auto",
            "subagent_acompact-recovered-boundary-1",
        ):
            with self.subTest(agent_name=agent_name):
                roles, report = self._export_roles(
                    agent_name, mask_compact_assistant_history=True
                )
                self.assertEqual(roles, expected_roles)
                self.assertTrue(report["mask_compact_assistant_history"])
                self.assertEqual(report["masked_assistant_messages"], 2)

    def test_enabled_flag_does_not_change_other_agents(self):
        roles, report = self._export_roles(
            "subagent_worker-1", mask_compact_assistant_history=True
        )

        self.assertEqual(
            roles,
            ["system", "user", "assistant", "user", "assistant", "user", "assistant"],
        )
        self.assertEqual(report["masked_assistant_messages"], 0)

    def test_cli_flag_is_opt_in(self):
        common = ["--input-dir", "input", "--output-dir", "output"]

        self.assertFalse(
            export_sharegpt_recovered.parse_args(common).mask_compact_assistant_history
        )
        self.assertTrue(
            export_sharegpt_recovered.parse_args(
                [*common, "--mask-compact-assistant-history"]
            ).mask_compact_assistant_history
        )


if __name__ == "__main__":
    unittest.main()
