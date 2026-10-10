"""Offline assertions over retained native compact evidence (no model calls)."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from api_protocols import normalize_response
from finalize import finalize_dataset
from export_sharegpt import export_sharegpt
from export_sharegpt_recovered import export_sharegpt_recovered


def verify(root, client):
    native = json.loads((root / "native_report.json").read_text())
    assert native.get("native_compact_applied") and not native.get("error"), native
    if client == "kimi":
        assert native.get("after_marker_observed"), "Post-compact markers absent"
    else:
        assert all(m in native.get("after", "") for m in ("BEFORE_COMPACT_OK", "AFTER_COMPACT_OK"))
    raw = root / "captures/raw"
    hashes = {str(p.relative_to(raw)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in raw.rglob("*") if p.is_file()}
    session = root / "client-home" / ("sessions" if client == "kimi" else "state.db")
    out = root / "verified_exports"
    final = finalize_dataset(root / "captures", session, out / "dataset", client=client)
    strict = export_sharegpt(out / "dataset", out / "strict", "separate")
    recovered = export_sharegpt_recovered(out / "dataset", out / "recovered", "separate")
    rows = [json.loads(l) for l in (out / "recovered/sharegpt.jsonl").read_text().splitlines()]
    messages = [m for row in rows for m in row["messages"]]
    captured = []
    summary_ids = set()
    for p in (raw / "completed").glob("*/request.json"):
        request = json.loads(p.read_text())
        body = request.get("body_json") or {}
        # Native title requests and /models probes are retained separately.
        text = json.dumps(body.get("messages", []), ensure_ascii=False)
        compact = ("You are about to run out of context. Create a handoff summary for the" in text
            or "You are a summarization agent creating a context checkpoint." in text)
        if not request.get("is_inference_request") or not (body.get("tools") or compact):
            continue
        response = json.loads((p.parent / "response.json").read_text())
        state = json.loads((p.parent / "state.json").read_text())
        assert state["state"] == "complete" and not response.get("aggregation_errors")
        captured.append(response["message_id"])
        if compact:
            summary_ids.add(response["message_id"])
        answer = normalize_response(response["message"], request["api_protocol"])
        for block in answer.get("content", []):
            if block["type"] == "thinking" and block.get("thinking"):
                assert any(m.get("reasoning_content") == block["thinking"] for m in messages), "Reasoning changed"
            elif block["type"] == "text" and block.get("text"):
                assert any(m.get("role") == "assistant" and block["text"] in m.get("content", "") for m in messages), "Text changed"
            elif block["type"] == "tool_use":
                assert any(t.get("id") == block["id"] and t.get("function", {}).get("name") == block["name"]
                    and (json.loads(t["function"]["arguments"]) if isinstance(t["function"]["arguments"], str)
                         else t["function"]["arguments"]) == block["input"]
                    for m in messages for t in m.get("tool_calls", [])), "Tool call changed"
    associations = [json.loads(p.read_text()) for p in (out / "dataset/tasks").rglob("response.json")]
    matched = {r["transport"]["message_id"] for r in associations}
    matched_summaries = {r["transport"]["message_id"] for r in associations
                        if r["association"]["association_method"].endswith("_compaction_boundary")}
    assert matched == set(captured), (matched, set(captured))
    assert summary_ids and matched_summaries == summary_ids
    assert final["conflicts"] == final["inflight"] == 0
    assert not strict.get("skipped_agents") and not recovered.get("skipped_agents")
    for marker in ("BEFORE_COMPACT_OK", "AFTER_COMPACT_OK"):
        assert any(m.get("role") == "tool" and marker in m.get("content", "") for m in messages), "Tool result lost"
    assert hashes == {str(p.relative_to(raw)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in raw.rglob("*") if p.is_file()}, "Raw evidence mutated during export"
    report = {"client": client, "model": native["model"], "native_compact_applied": True,
        "matched_inference_rounds": len(matched), "matched_compact_rounds": len(summary_ids),
        "main_rounds": len(matched - summary_ids), "sharegpt_records": len(rows),
        "assistant_text_reasoning_tools_preserved": True, "before_after_tool_results_preserved": True,
        "raw_evidence_unchanged": True, "finalization": final, "strict": strict, "recovered": recovered,
        "passed": True}
    (root / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--client", choices=["kimi", "hermes"], required=True)
    args = parser.parse_args()
    result = verify(args.root.resolve(), args.client)
    print(json.dumps({k: result[k] for k in ("client", "main_rounds", "matched_compact_rounds", "sharegpt_records", "passed")}))
