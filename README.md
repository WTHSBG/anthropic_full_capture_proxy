# Anthropic Full Capture Proxy

透明转发 Anthropic Messages、OpenAI Chat Completions 与 Responses API，完整保存每次 HTTP 请求、非流式响应、原始 SSE、SSE 事件和聚合响应。支持 Claude Code、Kimi Code CLI 与 NousResearch Hermes Agent，运行结束后整理为 `task / main agent / subagent / round` 数据集并导出 ShareGPT。

Claude Code 默认按 session `message.id` 关联；Kimi 优先使用 Wire 响应 ID；Hermes 使用 SQLite 中完整历史与响应的一对一唯一匹配。原生 compact 通过完成边界与摘要证据恢复归属，后继会话仍归同一任务。新客户端的配置、命令及支持边界见 [Kimi Code 与 Hermes 接入](README_07_Kimi_Code与Hermes.md)。

本项目不会修改请求 body、模型名称或认证方式。Harbor/Claude Code 发来的 `Authorization`、`X-Api-Key` 等认证头会原样转发给上游；采集文件中的密钥值会脱敏并只保存 SHA-256。

采集写盘与解析在有界后台任务中执行。采集故障或积压超限时继续转发，健康接口明确
报告失败，能保存的残缺记录保留在 `partial` / `inflight`。整理前等待后台采集完成并
核对失败计数，具体参数与检查方法见[安装并启动代理](README_01_安装并启动代理.md)。

可配置归档目录、执行间隔、最小保留时间与批量，在后台将已收尾采集打包为带哈希清单的
`tar.gz`。可选校验后清理本地原件，并提供离线恢复工具，见[自动归档](README_08_自动归档.md)。

## 按运行顺序阅读

1. [安装并启动代理](README_01_安装并启动代理.md)
2. [运行 Harbor](README_02_运行_Harbor.md)
3. [整理最终数据集](README_03_整理最终数据集.md)
4. [导出 ShareGPT SFT 数据](README_04_导出_ShareGPT.md)
5. [采集与关联机制](README_05_采集与关联机制.md)（参考）
6. [测试与运维](README_06_测试与运维.md)（参考）
7. [Kimi Code 与 Hermes 接入](README_07_Kimi_Code与Hermes.md)
8. [自动归档与恢复](README_08_自动归档.md)

## 独立扩展入口

| 入口 | 功能与说明 | 当前支持边界 |
| --- | --- | --- |
| [proxy_lb.py](README_proxy_lb.md) | 多后端轮询或随机分配 | 独立转发实现，后台采集隔离和自动归档尚未接入 |
| [proxy_retry.py](README_proxy_retry.md) | 完整接收、校验后回放，失败自动重试 | 模型重试面向 Messages；Chat Completions/Responses 重试、后台采集隔离和自动归档尚未接入 |
| [export_sharegpt_recovered_merged.py](README_export_sharegpt_recovered_merged.md) | 合并 compact 样本、过滤截断监督目标及按 CC session 分类超限失败 | Hermes/OpenAI 格式尚未接入；成功合并后历史 assistant 参与训练 |

上述入口可独立选择。下述工作流程及自动归档配置使用 `proxy.py`。
`proxy_retry.py` 的失败尝试保存在 `raw/retry_attempts/`，需要单独安排保存和清理。

## 工作流程

```text
Harbor / Claude Code / Kimi Code / Hermes
        │
        │ Messages / Chat Completions / Responses
        ▼
proxy.py
  ├─ 为每个请求生成独立 capture_id
  ├─ 原样转发请求和响应
  ├─ 保存原始 request/response
  └─ 流式响应同时保存 SSE 并聚合完整 Message
        │
        ▼
capture_logs/<run>/raw/{inflight,completed}
        │
        │ Harbor task 或整个 run 结束
        ▼
finalize.py / finalize-harbor.py / finalize-node.py
  ├─ 扫描主 session JSONL
  ├─ 扫描 subagents/agent-*.jsonl
  ├─ 响应 ID 或完整历史的一对一唯一关联
  └─ 生成最终 task/agent/round 数据集
        │
        │ 可选
        ▼
export_sharegpt.py / export_sharegpt_recovered.py
  ├─ 严格导出完整响应
  └─ 有重试或后续历史证据时恢复异常流式聚合
```

代理为每个 HTTP 请求独立采集其响应。task、主 agent、子 agent 的归属由整理脚本在运行结束后从客户端会话恢复；不需要给 LLM 地址增加任务前缀。有歧义或证据不足的记录保留在 conflicts/unmatched。

恢复版导出器的适用场景、证据要求和不可恢复边界见[导出 ShareGPT SFT 数据](README_04_导出_ShareGPT.md#恢复版的证据和边界)。

## 验证与支持边界

```bash
python -m unittest discover -s tests -v
```

180 项自动化测试覆盖原有 Claude Code 路径、新客户端关联/导出、compact、采集故障、
自动归档及上游新增的负载均衡、缓存重试与合并导出。各独立入口的支持边界见上表。
真实 Kimi/Hermes 文件工具及原生压缩已另行验收；受测版本和范围见
[验收说明](tests/ACCEPTANCE.md)。文本、可见 reasoning 和 function 工具可导出；
未知协议内容、缺失会话和非唯一证据保持未关联或拒绝有损导出。

压缩样本需保留原有历史 assistant mask 规则时，使用 `export_sharegpt_recovered.py`：最后一条摘要仍为 assistant，
此前 assistant 标为 masked_assistant；具体训练字段与规则见
[导出说明](README_04_导出_ShareGPT.md#kimi--hermes-与压缩轨迹)。

CC compact 关联同时兼容旧式 parentUuid 和新式 preservedSegment.anchorUuid；
离线导出识别 system 中的 total_tokens 运行时注入。格式与证据边界见
[关联机制](README_05_采集与关联机制.md)和[导出说明](README_04_导出_ShareGPT.md)。
