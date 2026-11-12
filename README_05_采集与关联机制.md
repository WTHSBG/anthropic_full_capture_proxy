# 05 采集与关联机制

[上一步：导出 ShareGPT](README_04_导出_ShareGPT.md) · [返回总览](README.md) · [下一篇：测试与运维](README_06_测试与运维.md)

本文是机制参考，不是运行前置步骤。

HTTP 转发不等待采集落盘。每个请求的后台队列依次保存请求、响应分片及收尾信息；
慢速写盘或采集异常不阻塞事件循环，积压超限时终止该条采集并在健康接口报告。
因此客户端收到最终回答后，仍需等待 `capture.active = 0` 才能进行离线整理。
采集失败时不能保证完整轨迹；能落盘的失败记录标记为残缺，不混入完整数据。

下文主要描述 Claude Code / Anthropic；Kimi Code、Hermes、OpenAI 协议的关联与聚合规则见
[客户端接入说明](README_07_Kimi_Code与Hermes.md)。

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

即使收到 `message_stop`，存在 SSE JSON 解析错误或聚合错误时也会标记 partial。

## 关联规则

Claude Code 的主要关联键：

```text
代理捕获的 response.message.id
                  ==
Harbor/Claude Code session 中 assistant.message.id
```

同一个 `message.id` 在 session 中出现多条 assistant 记录时，会合并为一个模型 round。主/子 agent 由 session 文件路径、`agentId` 和 `isSidechain` 共同判断，其中 `subagents/agent-*.jsonl` 文件路径优先。

Messages 请求识别兼容标准 `POST /v1/messages` 及网关添加前缀后的路径（例如 `POST /anthropic/v1/messages`）；`/v1/messages/count_tokens` 不会被误判。finalizer 也会按 method/path 重新识别旧 capture，因此修复前被记录成 `is_messages_request: false` 的前缀路径无需重采。

Claude Code 普通请求不使用相邻时间、客户端 IP 或 prompt 内容做关联。其补充路径是 Claude Code 没有写出 `subagents/agent-acompact-*.jsonl` 的自动压缩请求。此时必须同时满足：

- HTTP 请求是 Messages 路径且状态完整
- `x-claude-code-session-id` 与主 session 完全一致
- 最后一条 user message 包含 Claude Code 专用 compact 指令
- 主 session 存在 `compact_boundary` 及其 `isCompactSummary` synthetic user message
- HTTP 响应完成时间与边界相邻
- 响应 `<summary>` 正文完整出现在 synthetic message 中
- capture 与边界是一对一唯一匹配

全部满足后，最终数据集中会创建 `subagent_acompact-recovered-<boundary_uuid>`；它不会修改或伪造源目录中的 `subagents/*.jsonl`。任何证据缺失、冲突或多义时仍进入 `unmatched`。恢复统计写入 `finalization_report.json.compact_recovery`。

## OpenAI 协议和原生客户端关联

Chat Completions 流必须收到完整 `finish_reason` 和 `[DONE]`；Responses 流须收到
合法 `response.completed`。解析或聚合错误、终止标记之前断开仍标 partial。
客户端在有效终止标记后关闭连接时，完整聚合可保留为 complete，同时记录
`disconnect_after_complete` 和传输信息，不抹去断开事实。

Kimi 从新/旧 Wire 的模型响应 ID 索引普通轮次；Hermes 的数据库行 ID 不是模型
响应 ID，只能通过实际完整历史和响应的一对一唯一匹配关联。已知客户端存储/
重放变换只用于证据投影，原始 response 的正文、reasoning 和工具信息不变。

Kimi 原生摘要用完成边界、专用 prompt、全文和模型/system/tools 指纹联合匹配。
Hermes 使用原生 `_compressed_summary` 记录及受支持的精确摘要包装/任务说明变换；
被拒绝的摘要不会冒充已应用边界。候选冲突或同一边界被多个 capture 共用仍隔离。
后继会话是同一 lineage 的延续，不因 parent_session_id 单独判定为子 agent。

适配器不会使用 IP 或时间临近去猜 Kimi/Hermes 会话归属，具体格式、边界与限制见
[接入文档](README_07_Kimi_Code与Hermes.md)。

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
