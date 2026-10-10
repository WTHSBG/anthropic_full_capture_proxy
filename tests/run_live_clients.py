"""真实 CLI 进程 + TCP 代理 + 本地确定性上游的验收，不调用付费模型。

用法：python tests/run_live_clients.py --client kimi --proxy-python /path/to/python \
  --output-dir /path/to/evidence --case tool
Hermes 另传 --hermes-python 与 --hermes-source。输出原生会话和验收报告。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen

PROJECT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def clean_env():
    # HOME 原值不变，显式的客户端 data home 指向验收目录；不继承模型认证变量。
    return {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR", "SYSTEMROOT") if key in os.environ}


def run(args):
    root = args.output_dir.resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"验收输出目录必须为空: {root}")
    root.mkdir(parents=True, exist_ok=True)
    work = root / "work"
    work.mkdir()
    (work / "greeting.txt").write_text("LOCAL_FILE_OK\n", encoding="utf-8")
    marker = f"ACCEPTANCE_OK_{args.client}_{args.case}"
    requests = []
    tool_result_seen = False

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *values):
            pass

        def send_json(self, value, status=200):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.send_json({"object": "list", "data": [{"id": "acceptance-model", "object": "model", "owned_by": "local"}]})

        def do_POST(self):
            nonlocal tool_result_seen
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            tools = [t.get("function", t) for t in body.get("tools", [])]
            tool = next((t for t in tools if t.get("name", "").lower() in {"read", "readfile", "read_file"}), None)
            is_main = self.path.endswith("/chat/completions") and tool is not None
            requests.append({"path": self.path, "body": body, "is_main_inference": is_main})
            (root / "upstream_requests.json").write_text(json.dumps(requests, ensure_ascii=False, indent=2))
            print(f"upstream request {len(requests)}: {self.path}", flush=True)
            if not self.path.endswith("/chat/completions"):
                self.send_json({"error": "local acceptance upstream implements chat/completions only"}, 404)
                return
            messages = body.get("messages", [])
            results = [m for m in messages if m.get("role") == "tool"]
            if results:
                tool_result_seen = any("LOCAL_FILE_OK" in json.dumps(m) for m in results)
            call = None
            if args.case == "tool" and is_main and not results:
                properties = tool.get("parameters", {}).get("properties", {})
                key = "file_path" if "file_path" in properties else "path"
                call = {"id": "call_acceptance_read", "type": "function", "function": {
                    "name": tool["name"], "arguments": json.dumps({key: str(work / "greeting.txt")})}}
            native = {"id": f"chat_acceptance_{len(requests)}", "object": "chat.completion",
                "created": int(time.time()), "model": body.get("model", "acceptance-model"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": None if call else marker,
                    "reasoning_content": "Local validation reasoning.", **({"tool_calls": [call]} if call else {})},
                    "finish_reason": "tool_calls" if call else "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}
            if not body.get("stream"):
                self.send_json(native)
                return
            base = {"id": native["id"], "object": "chat.completion.chunk", "created": native["created"], "model": native["model"]}
            deltas = [{"role": "assistant", "reasoning_content": "Local validation reasoning."}]
            if call:
                arguments = call["function"]["arguments"]
                half = len(arguments) // 2
                deltas.extend([{"tool_calls": [{"index": 0, "id": call["id"], "type": "function",
                    "function": {"name": call["function"]["name"], "arguments": arguments[:half]}}]},
                    {"tool_calls": [{"index": 0, "function": {"arguments": arguments[half:]}}]}])
            else:
                deltas.extend([{"content": marker[:10]}, {"content": marker[10:]}])
            events = [{**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]} for delta in deltas]
            events.append({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": native["choices"][0]["finish_reason"]}]})
            events.append({**base, "choices": [], "usage": native["usage"]})
            raw = b"".join(("data: " + json.dumps(e) + "\n\n").encode() for e in events) + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            for offset in range(0, len(raw), 31):
                self.wfile.write(raw[offset:offset + 31])
                self.wfile.flush()

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=upstream.serve_forever, daemon=True)
    worker.start()
    proxy_port = free_port()
    proxy_log = (root / "proxy.log").open("w")
    proxy = subprocess.Popen([args.proxy_python, str(PROJECT / "proxy.py"), "--listen-host", "127.0.0.1",
        "--listen-port", str(proxy_port), "--upstream-url", f"http://127.0.0.1:{upstream.server_port}",
        "--log-dir", str(root / "captures")], stdout=proxy_log, stderr=subprocess.STDOUT, env=clean_env())
    try:
        for attempt in range(100):
            if proxy.poll() is not None:
                raise RuntimeError("代理启动失败，请检查 proxy.log")
            try:
                urlopen(f"http://127.0.0.1:{proxy_port}/healthz", timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("代理启动超时")
        home = root / "client-home"
        home.mkdir()
        env = clean_env()
        endpoint = f"http://127.0.0.1:{proxy_port}/v1"
        prompt = "仅返回本地验收结果。" if args.case == "text" else "读取 greeting.txt，并返回文件里的内容。"
        if args.client == "kimi":
            env["KIMI_CODE_HOME"] = str(home)
            env["KIMI_SHARE_DIR"] = str(home)
            (home / "config.toml").write_text(f'''default_model = "acceptance"
default_permission_mode = "auto"
telemetry = false
merge_all_available_skills = false
builtin_product_skills = false
[providers.local]
type = "openai"
base_url = "{endpoint}"
api_key = "acceptance-only"
[models.acceptance]
provider = "local"
model = "acceptance-model"
max_context_size = 100000
capabilities = ["tool_use", "thinking"]
''')
            (home / "tui.toml").write_text("[upgrade]\nauto_install = false\n")
            empty_skills = root / "empty-skills"
            empty_skills.mkdir()
            command = [args.kimi_bin, "--skills-dir", str(empty_skills), "--model", "acceptance", "--prompt", prompt]
            session_source = home / "sessions"
            version = subprocess.run([args.kimi_bin, "--version"], env=env, capture_output=True, text=True, timeout=15).stdout.strip()
        else:
            env["HERMES_HOME"] = str(home)
            env["HERMES_RUNTIME_DIR"] = str(root / "hermes-runtime")
            env["OPENAI_API_KEY"] = "acceptance-only"
            env["PYTHONPATH"] = str(args.hermes_source.resolve())
            (home / "config.yaml").write_text(f'''model:
  default: acceptance-model
  provider: custom
  base_url: {endpoint}
  api_key: acceptance-only
''')
            command = [args.hermes_python, "-m", "hermes_cli.main", "chat", "--provider", "custom",
                "--model", "acceptance-model", "--query", prompt, "--toolsets", "terminal,file",
                "--oneshot", "--ignore-rules", "--max-turns", "5", "--run-budget", "60"]
            session_source = home / "state.db"
            version = subprocess.run(["git", "-C", str(args.hermes_source), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        print(f"running actual {args.client}: {version}", flush=True)
        with (root / "client.log").open("w") as log:
            completed = subprocess.run(command, cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=120)
        report = {"client": args.client, "version_or_commit": version, "case": args.case,
            "upstream": "local_deterministic", "client_exit_code": completed.returncode,
            "http_requests": len(requests), "tool_result_seen": tool_result_seen,
            "session_source": str(session_source), "marker_seen": marker in (root / "client.log").read_text()}
        expected_ids = {f"chat_acceptance_{i}" for i, req in enumerate(requests, 1) if req["is_main_inference"]}
        report["main_inference_requests"] = len(expected_ids)
        report["expected_model_response_ids"] = sorted(expected_ids)
        if completed.returncode == 0 and requests:
            for name, command in (("finalization", ["finalize.py", "--client", args.client, "--capture-dir", str(root / "captures"),
                    "--session-dir", str(session_source), "--output-dir", str(root / "dataset")]),
                ("strict_export", ["export_sharegpt.py", "--input-dir", str(root / "dataset"), "--output-dir", str(root / "sharegpt_strict")]),
                ("export", ["export_sharegpt_recovered.py", "--input-dir", str(root / "dataset"), "--output-dir", str(root / "sharegpt")])):
                result = subprocess.run([args.proxy_python, str(PROJECT / command[0]), *command[1:]], capture_output=True, text=True, timeout=30)
                (root / f"{name}.log").write_text(result.stdout + result.stderr)
                report[f"{name}_exit_code"] = result.returncode
                if result.returncode == 0:
                    report[name] = json.loads(result.stdout)
        finalization = report.get("finalization", {})
        export = report.get("export", {})
        strict_export = report.get("strict_export", {})
        matched_ids = {json.loads(p.read_text()).get("transport", {}).get("message_id")
                       for p in (root / "dataset/tasks").glob("**/response.json")}
        report["matched_model_response_ids"] = sorted(i for i in matched_ids if i)
        combined = root / "sharegpt/sharegpt.jsonl"
        records = [json.loads(line) for line in combined.read_text().splitlines()] if combined.exists() else []
        exported_messages = [message for record in records for message in record.get("messages", [])]
        report["exported_reasoning_messages"] = sum(bool(m.get("reasoning_content")) for m in exported_messages)
        report["exported_tool_result_seen"] = any(m.get("role") == "tool" and "LOCAL_FILE_OK" in m.get("content", "") for m in exported_messages)
        report["passed"] = bool(report["marker_seen"] and completed.returncode == 0 and requests
            and expected_ids and matched_ids == expected_ids and not finalization.get("conflicts")
            and export.get("combined_jsonl_records", 0) > 0 and not export.get("skipped_agents")
            and report.get("strict_export_exit_code") == 0 and not strict_export.get("skipped_agents")
            and report["exported_reasoning_messages"] >= len(expected_ids)
            and (args.case != "tool" or tool_result_seen and report["exported_tool_result_seen"]))
        (root / "acceptance_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({key: report[key] for key in ("client", "case", "client_exit_code", "http_requests", "tool_result_seen", "marker_seen", "passed")}, ensure_ascii=False), flush=True)
        return 0 if report["passed"] else 1
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proxy.kill()
            proxy.wait()
        proxy_log.close()
        upstream.shutdown()
        upstream.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=("kimi", "hermes"), required=True)
    parser.add_argument("--case", choices=("text", "tool"), default="tool")
    parser.add_argument("--proxy-python", required=True)
    parser.add_argument("--kimi-bin", default="kimi")
    parser.add_argument("--hermes-python")
    parser.add_argument("--hermes-source", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parsed = parser.parse_args()
    if parsed.client == "hermes" and (not parsed.hermes_python or not parsed.hermes_source):
        parser.error("Hermes 验收需要 --hermes-python 与 --hermes-source")
    raise SystemExit(run(parsed))
