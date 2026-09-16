# 05 采集与关联机制

[上一步：导出 ShareGPT](README_04_导出_ShareGPT.md) · [返回总览](README.md) · [下一篇：测试与运维](README_06_测试与运维.md)

本文是机制参考，不是运行前置步骤。

## SSE 聚合规则

聚合器处理：

- `message_start`
- `content_block_start`
- `text_delta`
- `thinking_delta`
- `signature_delta`
- `input_json_delta`
- `citations_delta`
- `content_block_stop`
- `message_delta`
- `message_stop`

`input_json_delta.partial_json` 会先按 block index 拼接，等 block 结束后再解析，避免把中间的不完整 JSON 当成错误。

如果流中断或缺少 `message_stop`：

- 原始 SSE 仍完整保存到实际收到的位置
- 聚合结果标记 `aggregation_complete: false`
- `state.json` 标记 `partial`
- 后处理不会把它伪装成完整响应

## 关联规则

主要关联键：

```text
代理捕获的 response.message.id
                  ==
Harbor/Claude Code session 中 assistant.message.id
```

同一个 `message.id` 在 session 中出现多条 assistant 记录时，会合并为一个模型 round。主/子 agent 由 session 文件路径、`agentId` 和 `isSidechain` 共同判断，其中 `subagents/agent-*.jsonl` 文件路径优先。

Messages 请求识别兼容标准 `POST /v1/messages` 及网关添加前缀后的路径（例如 `POST /anthropic/v1/messages`）；`/v1/messages/count_tokens` 不会被误判。finalizer 也会按 method/path 重新识别旧 capture，因此修复前被记录成 `is_messages_request: false` 的前缀路径无需重采。

普通请求不使用相邻时间、客户端 IP 或 prompt 内容做关联。唯一例外是 Claude Code 没有写出 `subagents/agent-acompact-*.jsonl` 的自动压缩请求。此时必须同时满足：

- HTTP 请求是 Messages 路径且状态完整
- `x-claude-code-session-id` 与主 session 完全一致
- 最后一条 user message 包含 Claude Code 专用 compact 指令
- 主 session 存在 `compact_boundary` 及其 `isCompactSummary` synthetic user message
- HTTP 响应完成时间与边界相邻
- 响应 `<summary>` 正文完整出现在 synthetic message 中
- capture 与边界是一对一唯一匹配

全部满足后，最终数据集中会创建 `subagent_acompact-recovered-<boundary_uuid>`；它不会修改或伪造源目录中的 `subagents/*.jsonl`。任何证据缺失、冲突或多义时仍进入 `unmatched`。恢复统计写入 `finalization_report.json.compact_recovery`。

## 保证范围

能够保证：

- 已捕获的每个请求与其 HTTP 响应不会串位
- 标准完整 SSE 可以聚合成完整 Message
- 有唯一 `message.id` 且 session 已保存时，可精确归到 task 和具体 agent
- 缺失 acompact JSONL 时，只按上述一对一证据恢复压缩请求
- 冲突不会覆盖，统一进入 `conflicts`

不能绝对保证：

- 在生成 `message.id` 前就失败的请求属于哪个客户端
- Harbor 崩溃且没有保存 session 时的 task/agent 归属
- 上游服务重复使用同一个 `message.id` 时的自动归属
- Anthropic 服务端没有通过 API 返回的隐藏信息
