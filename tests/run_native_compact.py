"""Opt-in real native compact acceptance; isolate client homes and keep evidence.

Kimi uses its public ACP /compact command. Hermes invokes the CLI's chat and
/compress handler in process (same native agent and persistence implementation).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from run_vendor_acceptance import PROJECT, clean_env, running_proxy, save


async def kimi_run(args, root, env):
    log = (root / "client.log").open("wb")
    process = await asyncio.create_subprocess_exec(args.kimi_bin, "--skills-dir", str(root / "empty-skills"),
        "--model", "acceptance", "acp", cwd=root / "work", env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=log)
    pending = {}
    trace = (root / "acp.jsonl").open("w", encoding="utf-8")

    async def send(value):
        trace.write(json.dumps({"direction": "in", "message": value}, ensure_ascii=False) + "\n")
        trace.flush()
        process.stdin.write((json.dumps(value) + "\n").encode())
        await process.stdin.drain()

    async def reader():
        while line := await process.stdout.readline():
            try:
                message = json.loads(line)
            except ValueError:
                log.write(line)
                continue
            trace.write(json.dumps({"direction": "out", "message": message}, ensure_ascii=False) + "\n")
            trace.flush()
            if message.get("method") == "session/request_permission":
                options = message["params"]["options"]
                option = next(o for o in options if o["kind"] == "allow_once")
                await send({"jsonrpc": "2.0", "id": message["id"], "result": {
                    "outcome": {"outcome": "selected", "optionId": option["optionId"]}}})
            elif "id" in message and message["id"] in pending:
                pending[message["id"]].set_result(message)
        for future in pending.values():
            if not future.done():
                future.set_exception(RuntimeError("ACP exited before reply"))

    task = asyncio.create_task(reader())
    async def call(method, params):
        identifier = len(pending) + 1
        future = asyncio.get_running_loop().create_future()
        pending[identifier] = future
        await send({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
        message = await asyncio.wait_for(future, timeout=240)
        if "error" in message:
            raise RuntimeError(str(message["error"]))
        return message["result"]

    try:
        initialized = await call("initialize", {"protocolVersion": 1, "clientCapabilities": {},
            "clientInfo": {"name": "native-compact-acceptance", "version": "1"}})
        session = await call("session/new", {"cwd": str(root / "work"), "mcpServers": []})
        sid = session["sessionId"]
        report = {"initialize": initialized, "session": session, "interface": "Kimi ACP /compact"}
        async def prompt(text):
            return await call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]})
        report["before"] = await prompt("Call Read to read before.txt. Return its marker verbatim. Keep this marker for later. Only use Read.")
        journal_offsets = {p: len(p.read_text().splitlines()) for p in (root / "client-home/sessions").rglob("wire.jsonl")}
        report["compact_command"] = await prompt("/compact Keep the exact BEFORE_COMPACT_OK marker and file names. Summarize very briefly.")
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            events = []
            for path in (root / "client-home/sessions").rglob("wire.jsonl"):
                for line in path.read_text().splitlines()[journal_offsets.get(path, 0):]:
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    if value.get("type") in {"context.apply_compaction", "full_compaction.complete", "full_compaction.cancel"}:
                        events.append(value)
            if any(e["type"] == "full_compaction.complete" for e in events):
                break
            if any(e["type"] == "full_compaction.cancel" for e in events):
                raise RuntimeError("Native compact cancelled; inspect wire journal")
            await asyncio.sleep(.2)
        else:
            raise TimeoutError("Native compact completion not observed")
        report["compact_events"] = events
        after_offset = len((root / "acp.jsonl").read_text().splitlines())
        report["after"] = await prompt("Call Read to read after.txt. Return its marker and the previous BEFORE_COMPACT_OK marker verbatim. Only use Read.")
        # ACP end_turn can precede the journal's buffered persistence flush.
        await asyncio.sleep(1)
        updates = [json.loads(line)["message"] for line in (root / "acp.jsonl").read_text().splitlines()[after_offset:]]
        text = "".join(m["params"]["update"].get("content", {}).get("text", "") for m in updates
            if m.get("method") == "session/update" and m.get("params", {}).get("update", {}).get("sessionUpdate") == "agent_message_chunk")
        report["after_marker_observed"] = "AFTER_COMPACT_OK" in text and "BEFORE_COMPACT_OK" in text
        report["native_compact_applied"] = any(e["type"] == "context.apply_compaction" for e in events)
        return report
    finally:
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            process.terminate()
            await process.wait()
        await task
        log.close()
        trace.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", choices=["kimi", "hermes"], required=True)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--model", default="deepseek-v4.1-flash")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--kimi-bin", default="kimi")
    parser.add_argument("--hermes-python")
    parser.add_argument("--hermes-source", type=Path)
    parser.add_argument("--hermes-rotate", action="store_true")
    parser.add_argument("--hermes-auto", action="store_true")
    parser.add_argument("--hermes-threshold", type=int, default=10000)
    parser.add_argument("--kimi-context-size", type=int, default=131072)
    args = parser.parse_args()
    if args.client == "hermes" and (not args.hermes_python or not args.hermes_source):
        parser.error("Hermes requires --hermes-python and --hermes-source")
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    if args.token_file.stat().st_mode & 0o077:
        raise PermissionError("Token file must have private permissions (chmod 600)")
    token = args.token_file.read_text().strip()
    home = root / "client-home"
    home.mkdir()
    work = root / "work"
    work.mkdir()
    (root / "empty-skills").mkdir()
    # A substantial actual tool output gives Hermes a real summarizable middle.
    (work / "before.txt").write_text("BEFORE_COMPACT_OK\n" + "".join(
        f"Evidence row {i:03}: Historical detail retained in raw evidence. The requested first-line marker stays unchanged.\n"
        for i in range(100)))
    (work / "after.txt").write_text("AFTER_COMPACT_OK\n")
    env = clean_env()
    report = {"client": args.client, "model": args.model, "upstream": args.upstream}
    try:
        with running_proxy(root, args.upstream) as endpoint:
            if args.client == "kimi":
                env.update(KIMI_CODE_HOME=str(home), KIMI_SHARE_DIR=str(home))
                (home / "config.toml").write_text(f'''default_model = "acceptance"
default_permission_mode = "auto"
telemetry = false
merge_all_available_skills = false
builtin_product_skills = false
[providers.acceptance]
type = "anthropic"
base_url = "{endpoint}"
api_key = "{token}"
[models.acceptance]
provider = "acceptance"
model = "{args.model}"
max_context_size = {args.kimi_context_size}
max_output_size = 4096
capabilities = ["tool_use", "thinking"]
''')
                (home / "config.toml").chmod(0o600)
                (home / "tui.toml").write_text("[upgrade]\nauto_install = false\n")
                report.update(asyncio.run(kimi_run(args, root, env)))
            else:
                env.update(HERMES_HOME=str(home), HERMES_RUNTIME_DIR=str(root / "hermes-runtime"),
                    OPENAI_API_KEY=token, PYTHONPATH=str(args.hermes_source.resolve()))
                (home / "config.yaml").write_text(f'''model:
  default: {args.model}
  provider: custom
  base_url: {endpoint}/v1
  key_env: OPENAI_API_KEY
compression:
  enabled: false
  protect_first_n: 0
  protect_last_n: 0
  tail_mode: legacy
  threshold_tokens: {args.hermes_threshold}
  target_ratio: 0.1
  proactive_prune_min_result_chars: 100000
  in_place: {str(not args.hermes_rotate).lower()}
auxiliary:
  compression:
    provider: custom
    model: {args.model}
    base_url: {endpoint}/v1
    api_key_env: OPENAI_API_KEY
''')
                command = [os.path.abspath(args.hermes_python), str(PROJECT / "tests/native_hermes_compact.py"),
                    "--root", str(root), "--model", args.model, "--endpoint", endpoint]
                if args.hermes_auto:
                    command.extend(["--auto", "--auto-threshold", str(args.hermes_threshold)])
                with (root / "client.log").open("w") as log:
                    result = subprocess.run(command, cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=360)
                report["client_exit_code"] = result.returncode
                if result.returncode:
                    report["error"] = "Hermes native harness exited; inspect client.log"
                if (root / "native_report.json").exists():
                    report.update(json.loads((root / "native_report.json").read_text()))
    except Exception as exc:
        report["error"] = (type(exc).__name__ + ": " + str(exc)).replace(token, "<redacted>")
    finally:
        for path in root.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                if token.encode() in data:
                    path.write_bytes(data.replace(token.encode(), b"<redacted>"))
        continuation = (report.get("after_marker_observed") if args.client == "kimi"
            else all(m in (report.get("after") or "") for m in ("BEFORE_COMPACT_OK", "AFTER_COMPACT_OK")))
        report["passed"] = bool(report.get("native_compact_applied") and continuation
            and not report.get("error") and report.get("client_exit_code", 0) == 0)
        save(root / "native_report.json", report)
    print(json.dumps({k: report.get(k) for k in ("client", "native_compact_applied", "error")}), flush=True)
    return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
