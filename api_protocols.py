"""API 协议识别、流式聚合和离线格式转换。原始 HTTP 字节始终不变。"""
from __future__ import annotations

import copy
import json
from typing import Any, Optional

ANTHROPIC = "anthropic_messages"
CHAT = "openai_chat"
RESPONSES = "openai_responses"


def request_protocol(method: Any, path: Any) -> Optional[str]:
    if not isinstance(method, str) or method.upper() != "POST" or not isinstance(path, str):
        return None
    path = "/" + path.strip("/")
    for suffix, protocol in (("/v1/messages", ANTHROPIC), ("/chat/completions", CHAT), ("/responses", RESPONSES)):
        if path == suffix or path.endswith(suffix):
            return protocol
    return None


def infer_protocol(body: dict[str, Any], message: Optional[dict[str, Any]] = None) -> str:
    if "input" in body or (message and message.get("object") == "response"):
        return RESPONSES
    if message and "choices" in message:
        return CHAT
    if any(m.get("role") in {"system", "developer", "tool"} or "tool_calls" in m
           for m in body.get("messages", []) if isinstance(m, dict)):
        return CHAT
    tools = body.get("tools", [])
    if any(isinstance(t, dict) and "function" in t for t in tools):
        return CHAT
    return ANTHROPIC


class ChatCompletionAggregator:
    """保留原生 choices 结构；只有 finish_reason 和 [DONE] 均收到才完整。"""

    def __init__(self) -> None:
        self.message: dict[str, Any] = {"object": "chat.completion", "choices": []}
        self.complete = False
        self.errors: list[str] = []
        self._choices: dict[int, dict[str, Any]] = {}
        self._calls: dict[int, dict[int, dict[str, Any]]] = {}

    @property
    def message_id(self) -> Optional[str]:
        return self.message.get("id")

    def consume(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("error"):
            self.errors.append(json.dumps(payload["error"], ensure_ascii=False))
        for key in ("id", "model", "created", "usage", "system_fingerprint"):
            if payload.get(key) is not None:
                if key == "id" and self.message.get(key) not in (None, payload[key]):
                    self.errors.append("同一 SSE 中出现多个响应 id")
                self.message[key] = copy.deepcopy(payload[key])
        for choice in payload.get("choices", []):
            index = choice.get("index")
            if not isinstance(index, int) or index < 0:
                self.errors.append("choice 缺少合法 index")
                continue
            current = self._choices.setdefault(index, {
                "index": index, "message": {"role": "assistant", "content": ""}, "finish_reason": None,
            })
            message = current["message"]
            delta = choice.get("delta", {})
            for key in ("content", "reasoning_content", "reasoning", "refusal"):
                value = delta.get(key)
                if isinstance(value, str):
                    message[key] = message.get(key, "") + value
                elif value is not None:
                    self.errors.append(f"不支持的流式 delta.{key} 类型")
            for call in delta.get("tool_calls") or []:
                call_index = call.get("index")
                if not isinstance(call_index, int) or call_index < 0:
                    self.errors.append("tool_call 缺少合法 index")
                    continue
                target = self._calls.setdefault(index, {}).setdefault(call_index, {
                    "id": "", "type": "function", "function": {"name": "", "arguments": ""},
                })
                if call.get("id"):
                    target["id"] += call["id"]
                for key in ("name", "arguments"):
                    value = call.get("function", {}).get(key)
                    if isinstance(value, str):
                        target["function"][key] += value
            if choice.get("finish_reason") is not None:
                current["finish_reason"] = choice["finish_reason"]

    def mark_done(self) -> None:
        if not self._choices or any(c["finish_reason"] is None for c in self._choices.values()):
            self.errors.append("[DONE] 前没有完整的 finish_reason")
            return
        for calls in self._calls.values():
            for call in calls.values():
                try:
                    if not call["id"] or not call["function"]["name"]:
                        raise ValueError("缺少工具 id/name")
                    if not isinstance(json.loads(call["function"]["arguments"]), dict):
                        raise ValueError("工具参数不是对象")
                except (ValueError, TypeError) as exc:
                    self.errors.append(f"tool_call 不完整: {exc}")
        self.complete = not self.errors

    def snapshot(self) -> dict[str, Any]:
        result = copy.deepcopy(self.message)
        result["choices"] = []
        for index, choice in sorted(self._choices.items()):
            value = copy.deepcopy(choice)
            if index in self._calls:
                value["message"]["tool_calls"] = [copy.deepcopy(c) for _, c in sorted(self._calls[index].items())]
            result["choices"].append(value)
        return result


class ResponsesAggregator:
    """response.completed 的完整 response 是事实源，断流保留已收到的项目。"""

    def __init__(self) -> None:
        self.message: Optional[dict[str, Any]] = None
        self.complete = False
        self.errors: list[str] = []
        self._items: dict[int, dict[str, Any]] = {}

    @property
    def message_id(self) -> Optional[str]:
        return self.message.get("id") if self.message else None

    def consume(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        kind = payload.get("type", "")
        response = payload.get("response")
        if isinstance(response, dict):
            self.message = copy.deepcopy(response)
        if kind == "response.completed":
            if not isinstance(response, dict) or response.get("status") != "completed":
                self.errors.append("response.completed 缺少 completed response")
            else:
                self.complete = not self.errors
        elif kind in {"error", "response.failed", "response.incomplete"}:
            self.errors.append(json.dumps(payload, ensure_ascii=False))
        elif kind in {"response.output_item.added", "response.output_item.done"}:
            if isinstance(payload.get("output_index"), int) and isinstance(payload.get("item"), dict):
                self._items[payload["output_index"]] = copy.deepcopy(payload["item"])
        elif kind == "response.function_call_arguments.delta":
            item = self._items.get(payload.get("output_index"))
            if item is not None:
                item["arguments"] = item.get("arguments", "") + payload.get("delta", "")
        elif kind == "response.output_text.delta":
            item = self._items.get(payload.get("output_index"))
            index = payload.get("content_index", 0)
            if item is not None and isinstance(index, int) and index >= 0:
                content = item.setdefault("content", [])
                while len(content) <= index:
                    content.append({"type": "output_text", "text": ""})
                content[index]["text"] += payload.get("delta", "")

    def snapshot(self) -> Optional[dict[str, Any]]:
        result = copy.deepcopy(self.message)
        if result is not None and not self.complete and self._items:
            result["output"] = [copy.deepcopy(item) for _, item in sorted(self._items.items())]
        return result


def text_parts(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        return [{"type": "text", "text": value}] if value else []
    if not isinstance(value, list):
        raise ValueError("content 必须是字符串或 block 列表")
    result = []
    for part in value:
        kind = part.get("type")
        if kind in {"text", "input_text", "output_text"}:
            result.append({"type": "text", "text": part["text"]})
        elif kind in {"think", "thinking"}:
            result.append({"type": "thinking", "thinking": part.get("think", part.get("thinking", ""))})
        else:
            # 不悄悄丢弃图片、音频或未知块，交由严格导出器拒绝。
            result.append(copy.deepcopy(part))
    return result


def function_block(call: dict[str, Any]) -> dict[str, Any]:
    function = call.get("function", call)
    arguments = function.get("arguments", "{}")
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    if not isinstance(arguments, dict):
        raise ValueError("工具 arguments 必须为 JSON object")
    return {"type": "tool_use", "id": call.get("call_id", call.get("id")),
            "name": function.get("name"), "input": arguments}


def chat_message(message: dict[str, Any]) -> dict[str, Any]:
    role = message.get("role")
    if role == "tool":
        return {"role": "user", "content": [{"type": "tool_result",
                "tool_use_id": message.get("tool_call_id"), "content": text_parts(message.get("content"))}]}
    content = text_parts(message.get("content"))
    if role == "assistant":
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            content.insert(0, {"type": "thinking", "thinking": reasoning})
        if message.get("refusal"):
            content.append({"type": "text", "text": message["refusal"]})
        for call in message.get("tool_calls") or []:
            content.append(function_block(call))
    return {"role": role, "content": content}


def responses_messages(items: Any) -> list[dict[str, Any]]:
    if isinstance(items, str):
        return [{"role": "user", "content": text_parts(items)}]
    if not isinstance(items, list):
        raise ValueError("Responses input/output 必须为字符串或数组")
    result: list[dict[str, Any]] = []
    for item in items:
        kind = item.get("type", "message")
        if kind == "message":
            message = {"role": item.get("role", "assistant"), "content": text_parts(item.get("content"))}
            if message["role"] == "assistant" and result and result[-1]["role"] == "assistant":
                result[-1]["content"].extend(message["content"])
            else:
                result.append(message)
        elif kind in {"function_call", "reasoning"}:
            if kind == "function_call":
                block = function_block(item)
            else:
                summary = item.get("summary", [])
                if item.get("encrypted_content") and not summary:
                    raise ValueError("无法无损导出仅包含 encrypted_content 的 reasoning")
                block = {"type": "thinking", "thinking": "\n".join(p["text"] for p in summary)}
            if result and result[-1]["role"] == "assistant":
                result[-1]["content"].append(block)
            else:
                result.append({"role": "assistant", "content": [block]})
        elif kind == "function_call_output":
            result.append({"role": "user", "content": [{"type": "tool_result",
                "tool_use_id": item.get("call_id"), "content": text_parts(item.get("output"))}]})
        else:
            raise ValueError(f"不支持的 Responses item: {kind}")
    return result


def normalize_request(body: dict[str, Any], protocol: str) -> dict[str, Any]:
    if protocol == ANTHROPIC:
        return copy.deepcopy(body)
    result = copy.deepcopy(body)
    if protocol == RESPONSES:
        if body.get("previous_response_id"):
            raise ValueError("含 previous_response_id 的请求没有自包含历史，不能直接导出")
        messages = responses_messages(body.get("input", []))
        system = text_parts(body.get("instructions"))
    elif protocol == CHAT:
        messages = [chat_message(m) for m in body.get("messages", [])]
        system = []
    else:
        raise ValueError(f"未知 API 协议: {protocol}")
    history = []
    for message in messages:
        if message["role"] in {"system", "developer"}:
            if history:
                raise ValueError("历史中途的 system/developer 消息不能合并到开头")
            system.extend(message["content"])
        else:
            history.append(message)
    tools = []
    for tool in body.get("tools", []):
        if tool.get("type") != "function":
            raise ValueError(f"不支持的工具类型: {tool.get('type')}")
        function = tool.get("function", tool)
        tools.append({"name": function["name"], "description": function.get("description", ""),
                      "input_schema": copy.deepcopy(function.get("parameters", {}))})
    result.update(messages=history, system=system, tools=tools)
    return result


def normalize_response(message: dict[str, Any], protocol: str) -> dict[str, Any]:
    if protocol == ANTHROPIC:
        return copy.deepcopy(message)
    if protocol == CHAT:
        choices = message.get("choices", [])
        if len(choices) != 1:
            raise ValueError("训练轨迹要求恰好一个 completion choice")
        result = chat_message(choices[0]["message"])
        finish = choices[0].get("finish_reason")
    elif protocol == RESPONSES:
        parts = responses_messages(message.get("output", []))
        if any(p["role"] != "assistant" for p in parts):
            raise ValueError("Responses output 包含非 assistant 消息")
        result = {"role": "assistant", "content": [b for p in parts for b in p["content"]]}
        finish = "tool_calls" if any(b["type"] == "tool_use" for b in result["content"]) else "stop"
    else:
        raise ValueError(f"未知 API 协议: {protocol}")
    result.update(id=message.get("id"), model=message.get("model"), type="message",
                  stop_reason={"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}.get(finish, finish))
    return result


def normalize_round(request: dict[str, Any], response: dict[str, Any], transport: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    protocol = transport.get("api_protocol") or infer_protocol(request, response)
    return normalize_request(request, protocol), normalize_response(response, protocol)
