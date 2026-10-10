"""Drive the actual Hermes CLI methods without a terminal renderer/input loop."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--auto-threshold", type=int, default=7000)
    args = parser.parse_args()
    from cli import HermesCLI

    cli = HermesCLI(model=args.model, provider="custom", base_url=args.endpoint + "/v1",
        toolsets=["terminal", "file"], max_turns=4, run_budget=10, ignore_rules=True)
    report = {"interface": "HermesCLI.chat / _manual_compress (native CLI handler)"}
    report["before"] = cli.chat("Call read_file to read before.txt. Return only its first-line marker verbatim. Keep this marker for later. Only use read_file.")
    report["checkpoint"] = cli.chat("Reply CHECKPOINT_OK and the previous first-line marker. Do not repeat historical detail, and do not call tools.")
    report["checkpoint_second"] = cli.chat("Reply SECOND_CHECKPOINT_OK and BEFORE_COMPACT_OK. Do not call tools.")
    report["session_before"] = cli.session_id
    report["history_before_count"] = len(cli.conversation_history)
    report["compression_before"] = cli.agent.context_compressor.compression_count
    before_snapshot = json.dumps(cli.conversation_history, sort_keys=True)
    # The handler handles history replacement, DB commit and continuation adoption.
    if not args.auto:
        cli._manual_compress("/compress Preserve BEFORE_COMPACT_OK and file names. Summarize in fewer than 100 words.")
    else:
        # Arm native preflight only after enough real history exists; early
        # four-message no-ops otherwise engage Hermes's structural backoff.
        cli.agent.compression_enabled = True
        cli.agent.context_compressor.threshold_tokens_cap = args.auto_threshold
        cli.agent.context_compressor.threshold_tokens = args.auto_threshold
        report["forced_auto_threshold"] = args.auto_threshold
    report["trigger"] = "automatic" if args.auto else "manual"
    report["session_after"] = cli.session_id
    report["history_after_count"] = len(cli.conversation_history)
    report["compression_after"] = cli.agent.context_compressor.compression_count
    report["native_compact_applied"] = (report["compression_after"] > report["compression_before"]
        and json.dumps(cli.conversation_history, sort_keys=True) != before_snapshot)
    (args.root / "history_after_compact.json").write_text(json.dumps(cli.conversation_history, ensure_ascii=False, indent=2))
    report["after"] = cli.chat("Call read_file to read after.txt. Return its first-line marker and the previous BEFORE_COMPACT_OK marker verbatim. Only use read_file.")
    cli._persist_active_session_before_close()
    if args.auto:
        import sqlite3
        with sqlite3.connect(args.root / "client-home/state.db") as db:
            report["native_summary_rows"] = db.execute("SELECT count(*) FROM messages WHERE _compressed_summary=1").fetchone()[0]
        report["native_compact_applied"] = report["native_summary_rows"] > 0
    report["final_history_count"] = len(cli.conversation_history)
    (args.root / "native_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    assert report["native_compact_applied"], "Native compression did not change context"
    assert "AFTER_COMPACT_OK" in (report["after"] or ""), "Post-compact turn failed"


if __name__ == "__main__":
    main()
