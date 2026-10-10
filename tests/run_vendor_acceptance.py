"""Opt-in live vendor acceptance. Credentials are read from a private file.

wire: compare both sides of one live transaction via a local observation server.
clients: execute real Kimi/Hermes file tools and verify native-session exports.
Never compare two independent model generations as if they were identical bytes.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen

import httpx

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from api_protocols import normalize_response
from proxy import HOP_BY_HOP_HEADERS, REQUEST_HEADERS_TO_DROP


def digest(value):
    return hashlib.sha256(value).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def clean_env():
    return {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR") if key in os.environ}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def running_proxy(root, upstream):
    root.mkdir(parents=True, exist_ok=True)
    port = free_port()
    with (root / "proxy.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, str(PROJECT / "proxy.py"),
            "--listen-host", "127.0.0.1", "--listen-port", str(port),
            "--upstream-url", upstream, "--log-dir", str(root / "captures")],
            stdout=log, stderr=subprocess.STDOUT, env=clean_env())
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Proxy exited; inspect proxy.log")
                try:
                    urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1).close()
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise TimeoutError("Proxy did not start")
            yield f"http://127.0.0.1:{port}"
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


async def wire_cases(args, root, token):
    observations = {}

    class Observer(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *_):
            pass

        def do_POST(self):
            case = self.headers["x-acceptance-case"]
            folder = root / case
            folder.mkdir(parents=True, exist_ok=True)
            body = self.rfile.read(int(self.headers.get("content-length", "0")))
            (folder / "upstream_request.body").write_bytes(body)
            incoming = list(self.headers.items())
            record = {"path": self.path, "request_sha256": digest(body),
                "authorization_preserved": self.headers.get("authorization") == "Bearer " + token,
                "version_header": self.headers.get("anthropic-version"),
                "response_chunks": 0}
            observations[case] = record
            headers = [(k, v) for k, v in incoming if k.lower() not in REQUEST_HEADERS_TO_DROP]
            try:
                with httpx.Client(timeout=httpx.Timeout(120, connect=15), trust_env=False) as client:
                    with client.stream("POST", args.upstream.rstrip("/") + self.path,
                                       headers=headers, content=body) as response:
                        record["status"] = response.status_code
                        record["response_headers"] = [(k, v) for k, v in response.headers.multi_items()
                            if k.lower() not in {"authorization", "x-api-key", "set-cookie"}]
                        self.send_response(response.status_code)
                        for key, value in response.headers.multi_items():
                            if key.lower() not in HOP_BY_HOP_HEADERS:
                                self.send_header(key, value)
                        self.end_headers()
                        with (folder / "upstream_response.body").open("wb") as output:
                            for chunk in response.iter_raw():
                                if "first_upstream_chunk_at" not in record:
                                    record["first_upstream_chunk_at"] = time.monotonic()
                                record["response_chunks"] += 1
                                output.write(chunk)
                                self.wfile.write(chunk)
                                self.wfile.flush()
                        record["upstream_finished_at"] = time.monotonic()
            except Exception as exc:
                record["error"] = (type(exc).__name__ + ": " + str(exc)).replace(token, "<redacted>")
            finally:
                record["finished"] = True
                save(folder / "upstream_observation.json", record)

    observer = ThreadingHTTPServer(("127.0.0.1", 0), Observer)
    observer.daemon_threads = True
    threading.Thread(target=observer.serve_forever, daemon=True).start()
    results = []
    semaphore = asyncio.Semaphore(3)
    try:
        with running_proxy(root, f"http://127.0.0.1:{observer.server_port}") as endpoint:
            async def one(model, protocol, stream):
                async with semaphore:
                    case = f"{model}_{protocol}_{'stream' if stream else 'json'}"
                    folder = root / case
                    folder.mkdir(parents=True, exist_ok=True)
                    body = {"model": model, "max_tokens": 512, "stream": stream,
                        "messages": [{"role": "user", "content": "只回复 WIRE_OK_中文，不要调用工具。"}]}
                    raw = json.dumps(body, ensure_ascii=False, indent=2).encode()
                    path = "/v1/messages" if protocol == "anthropic" else "/v1/chat/completions"
                    headers = {"authorization": "Bearer " + token, "content-type": "application/json",
                        "anthropic-version": "2023-06-01", "x-acceptance-case": case}
                    result = {"case": case, "model": model, "protocol": protocol, "stream": stream}
                    (folder / "client_request.body").write_bytes(raw)
                    try:
                        async with httpx.AsyncClient(timeout=150, trust_env=False) as client:
                            async with client.stream("POST", endpoint + path, content=raw, headers=headers) as response:
                                result["client_status"] = response.status_code
                                result["client_headers"] = list(response.headers.multi_items())
                                chunks = []
                                async for chunk in response.aiter_raw():
                                    if "first_client_chunk_at" not in result:
                                        result["first_client_chunk_at"] = time.monotonic()
                                    chunks.append(chunk)
                                received = b"".join(chunks)
                                (folder / "client_response.body").write_bytes(received)
                        for _ in range(100):
                            if observations.get(case, {}).get("finished"):
                                break
                            await asyncio.sleep(0.05)
                        for _ in range(100):
                            captures = [p for p in (root / "captures/raw/completed").glob("*/request.json")
                                if any(h["name"].lower() == "x-acceptance-case" and h["value"] == case
                                       for h in json.loads(p.read_text())["headers"])]
                            if captures:
                                break
                            await asyncio.sleep(0.05)
                        capture = captures[0].parent
                        observed = observations[case]
                        actual_request = (folder / "upstream_request.body").read_bytes()
                        actual_response = (folder / "upstream_response.body").read_bytes()
                        meta = json.loads((capture / "response.json").read_text())
                        result.update({"capture_id": capture.name,
                            "request_bytes_equal": raw == actual_request == (capture / "request.body").read_bytes(),
                            "response_bytes_equal": received == actual_response == (capture / "response.body").read_bytes(),
                            "request_sha256": digest(raw), "response_sha256": digest(received),
                            "authorization_preserved": observed["authorization_preserved"],
                            "path_preserved": observed["path"] == path,
                            "version_header_preserved": observed["version_header"] == "2023-06-01",
                            "status_preserved": observed["status"] == result["client_status"],
                            "capture_state": json.loads((capture / "state.json").read_text())["state"],
                            "aggregation_errors": meta.get("aggregation_errors"),
                            "model_available": response.status_code == 200,
                            "upstream_chunks": observed["response_chunks"]})
                        expected_headers = [(k.lower(), v) for k, v in observed["response_headers"]
                            if k.lower() not in HOP_BY_HOP_HEADERS | {"server", "date"}]
                        result["response_headers_preserved"] = all(
                            response.headers.get_list(k) == [v for name, v in expected_headers if name == k]
                            for k in {name for name, _ in expected_headers})
                        if "first_upstream_chunk_at" in observed:
                            result["first_chunk_relay_ms"] = round(1000 * (result["first_client_chunk_at"] - observed["first_upstream_chunk_at"]), 2)
                            result["received_before_upstream_finished"] = result["first_client_chunk_at"] < observed["upstream_finished_at"]
                        result["relay_passed"] = all(result[k] for k in (
                            "request_bytes_equal", "response_bytes_equal", "authorization_preserved",
                            "path_preserved", "version_header_preserved", "status_preserved", "response_headers_preserved"))
                        result["aggregation_passed"] = not result["model_available"] or (
                            result["capture_state"] == "complete" and not result["aggregation_errors"])
                    except Exception as exc:
                        result["error"] = (type(exc).__name__ + ": " + str(exc)).replace(token, "<redacted>")
                        result["relay_passed"] = False
                    save(folder / "report.json", result)
                    results.append(result)
                    print(json.dumps({k: result.get(k) for k in ("case", "client_status", "relay_passed", "aggregation_passed", "capture_state", "error")}), flush=True)
            await asyncio.gather(*(one(m, p, s) for m in args.models for p in ("anthropic", "chat") for s in (False, True)))
    finally:
        observer.shutdown()
        observer.server_close()
    report = {"upstream": args.upstream, "cases": results,
        "all_relay_passed": all(r.get("relay_passed") for r in results),
        "all_aggregation_passed": all(r.get("aggregation_passed") for r in results)}
    save(root / "report.json", report)
    return report["all_relay_passed"] and report["all_aggregation_passed"]


def native_case(args, root, token, client, model):
    root.mkdir(parents=True, exist_ok=False)
    home = root / "client-home"
    work = root / "work"
    home.mkdir()
    work.mkdir()
    marker = f"LIVE_FILE_OK_{client}_{model}"
    (work / "greeting.txt").write_text(marker + "\n", encoding="utf-8")
    env = clean_env()
    tool_name = "Read" if client == "kimi" else "read_file"
    prompt = f"Call the {tool_name} tool to read the real file greeting.txt in the current directory. You must execute the tool before answering. Return the actual file contents verbatim. Do not guess, and do not use any other tools."
    report = {"client": client, "model": model, "upstream": args.upstream}
    with running_proxy(root, args.upstream) as endpoint:
        if client == "kimi":
            env["KIMI_CODE_HOME"] = str(home)
            env["KIMI_SHARE_DIR"] = str(home)
            env["ACCEPTANCE_API_KEY"] = token
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
model = "{model}"
max_context_size = 32768
capabilities = ["tool_use", "thinking"]
''', encoding="utf-8")
            (home / "config.toml").chmod(0o600)
            (home / "tui.toml").write_text("[upgrade]\nauto_install = false\n", encoding="utf-8")
            empty = root / "empty-skills"
            empty.mkdir()
            command = [args.kimi_bin, "--skills-dir", str(empty), "--model", "acceptance", "--prompt", prompt]
            session = home / "sessions"
            report["protocol"] = "anthropic"
            report["version"] = subprocess.check_output([args.kimi_bin, "--version"], env=env, text=True).strip()
        else:
            env["HERMES_HOME"] = str(home)
            env["HERMES_RUNTIME_DIR"] = str(root / "hermes-runtime")
            env["OPENAI_API_KEY"] = token
            env["PYTHONPATH"] = str(args.hermes_source.resolve())
            (home / "config.yaml").write_text(f'''model:
  default: {model}
  provider: custom
  base_url: {endpoint}/v1
  key_env: OPENAI_API_KEY
''', encoding="utf-8")
            command = [os.path.abspath(args.hermes_python), "-m", "hermes_cli.main", "chat", "--provider", "custom",
                "--model", model, "--query", prompt, "--toolsets", "terminal,file",
                "--oneshot", "--ignore-rules", "--max-turns", "4", "--run-budget", "10"]
            session = home / "state.db"
            report["protocol"] = "chat"
            report["version"] = subprocess.check_output(["git", "-C", str(args.hermes_source), "rev-parse", "HEAD"], text=True).strip()
        try:
            with (root / "client.log").open("w", encoding="utf-8") as log:
                result = subprocess.run(command, cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=180)
                report["client_exit_code"] = result.returncode
        except subprocess.TimeoutExpired:
            report["client_exit_code"] = "timeout"
        log_text = (root / "client.log").read_text(encoding="utf-8", errors="replace")
        (root / "client.log").write_text(log_text.replace(token, "<redacted>"), encoding="utf-8")
        if client == "kimi":
            config = home / "config.toml"
            config.write_text(config.read_text().replace(token, "<redacted>"), encoding="utf-8")
        report["marker_in_client_output"] = marker in log_text
    captures = []
    for path in (root / "captures/raw/completed").glob("*/request.json"):
        req = json.loads(path.read_text())
        if req.get("is_inference_request") and req.get("body_json", {}).get("tools"):
            response = json.loads((path.parent / "response.json").read_text())
            captures.append((path.parent, req, response))
    report["main_inference_count"] = len(captures)
    report["main_states"] = [json.loads((p / "state.json").read_text())["state"] for p, _, _ in captures]
    expected_ids = {r.get("message_id") for _, _, r in captures}
    report["expected_response_ids"] = sorted(i for i in expected_ids if i)
    report["tool_result_in_request"] = any(marker in json.dumps(q.get("body_json", {}).get("messages", [])) for _, q, _ in captures)
    if session.exists():
        for name, script, input_args in [
            ("finalization", "finalize.py", ["--client", client, "--capture-dir", str(root / "captures"), "--session-dir", str(session)]),
            ("strict", "export_sharegpt.py", ["--input-dir", str(root / "dataset")]),
            ("recovered", "export_sharegpt_recovered.py", ["--input-dir", str(root / "dataset")])]:
            out = root / ("dataset" if name == "finalization" else name)
            result = subprocess.run([sys.executable, str(PROJECT / script), *input_args, "--output-dir", str(out)], capture_output=True, text=True, timeout=60)
            (root / f"{name}.log").write_text((result.stdout + result.stderr).replace(token, "<redacted>"), encoding="utf-8")
            report[name + "_exit_code"] = result.returncode
            if result.returncode == 0:
                report[name] = json.loads(result.stdout)
    matched = {json.loads(p.read_text()).get("transport", {}).get("message_id") for p in (root / "dataset/tasks").glob("**/response.json")}
    report["matched_response_ids"] = sorted(i for i in matched if i)
    records = root / "recovered/sharegpt.jsonl"
    exported = [json.loads(line) for line in records.read_text().splitlines()] if records.exists() else []
    messages = [m for row in exported for m in row.get("messages", [])]
    report["exported_tool_result"] = any(m.get("role") == "tool" and marker in m.get("content", "") for m in messages)
    preserved = []
    for _, request, response in captures:
        try:
            normalized = normalize_response(response.get("message") or {}, request["api_protocol"])
            for block in normalized.get("content", []):
                if block.get("type") == "thinking" and block.get("thinking"):
                    preserved.append(any(block["thinking"] == m.get("reasoning_content") for m in messages))
                elif block.get("type") == "text" and block.get("text"):
                    preserved.append(any(block["text"] in m.get("content", "") for m in messages if m.get("role") == "assistant"))
                elif block.get("type") == "tool_use":
                    preserved.append(any(t.get("id") == block.get("id") and t.get("function", {}).get("name") == block.get("name")
                        and (json.loads(t["function"]["arguments"]) if isinstance(t.get("function", {}).get("arguments"), str)
                             else t.get("function", {}).get("arguments")) == block.get("input")
                        for m in messages for t in m.get("tool_calls", [])))
        except Exception as exc:
            report["content_check_error"] = str(exc).replace(token, "<redacted>")
            preserved.append(False)
    report["captured_assistant_content_preserved"] = bool(preserved) and all(preserved)
    report["passed"] = bool(report["client_exit_code"] == 0 and report["marker_in_client_output"]
        and len(captures) >= 2 and expected_ids and None not in expected_ids and matched == expected_ids
        and all(s == "complete" for s in report["main_states"])
        and report["tool_result_in_request"] and report["exported_tool_result"] and report["captured_assistant_content_preserved"]
        and report.get("strict_exit_code") == 0 and not report.get("strict", {}).get("skipped_agents")
        and not report.get("recovered", {}).get("skipped_agents") and not report.get("finalization", {}).get("conflicts"))
    save(root / "report.json", report)
    print(json.dumps({k: report.get(k) for k in ("client", "model", "client_exit_code", "main_inference_count", "main_states", "captured_assistant_content_preserved", "passed")}), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("wire", "clients"), required=True)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--client", choices=("kimi", "hermes"))
    parser.add_argument("--kimi-bin", default="kimi")
    parser.add_argument("--hermes-python")
    parser.add_argument("--hermes-source", type=Path)
    args = parser.parse_args()
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        parser.error("output-dir must be empty")
    if args.phase == "clients" and (not args.client or args.client == "hermes" and (not args.hermes_python or not args.hermes_source)):
        parser.error("clients requires --client and Hermes runtime paths when applicable")
    if args.token_file.stat().st_mode & 0o077:
        parser.error("token-file must have private permissions (chmod 600)")
    token = args.token_file.read_text().strip()
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if args.phase == "wire":
        passed = asyncio.run(wire_cases(args, root, token))
    else:
        reports = [native_case(args, root / f"{args.client}_{model}", token, args.client, model) for model in args.models]
        passed = all(r["passed"] for r in reports)
        save(root / "report.json", {"cases": reports, "all_passed": passed})
    raise SystemExit(0 if passed else 1)
