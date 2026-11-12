# Anthropic Full Capture Proxy

本 fork 的 `main` 基于上游 `b4e0874`，集成 `sharegpt-export-kit(1).tar.gz` 的 CC 整理与导出代码。
代码来源与逐文件 SHA-256 见 [EXPORT_KIT_SOURCE.json](EXPORT_KIT_SOURCE.json)。本分支面向 CC/Harbor；
已有 Kimi/Hermes 和自动归档扩展保留在独立分支。

透明转发 Anthropic Messages API，完整保存每次 HTTP 请求、非流式响应、原始 SSE、SSE 事件和聚合后的完整 Message。Harbor/Claude Code 运行结束后，再使用 session JSONL 按 `message.id` 整理成 `task / main agent / subagent / round` 数据集。

本项目不会修改请求 body、模型名称或认证方式。Harbor/Claude Code 发来的 `Authorization`、`X-Api-Key` 等认证头会原样转发给上游；采集文件中的密钥值会脱敏并只保存 SHA-256。

## 按运行顺序阅读

1. [安装并启动代理](README_01_安装并启动代理.md)
2. [运行 Harbor](README_02_运行_Harbor.md)
3. [整理最终数据集](README_03_整理最终数据集.md)
4. [固定入口导出教程](TUTORIAL.md) · [导出器详细说明](README_04_导出_ShareGPT.md)
5. [采集与关联机制](README_05_采集与关联机制.md)（参考）
6. [测试与运维](README_06_测试与运维.md)（参考）

## 工作流程

```text
多个 Harbor / Claude Code
        │
        │ POST /v1/messages
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
  ├─ response.message.id == session message.id
  └─ 生成最终 task/agent/round 数据集
        │
        │ 可选
        ▼
run_canonical_export.py
  ├─ 校验固定导出依赖指纹
  ├─ 恢复响应、合并 compact、处理截断监督目标
  └─ 保留含图上下文中首图之前的文本前缀
```

代理只负责保证每个 HTTP 请求和它自己的响应不串。task、主 agent、子 agent 的语义归属由整理脚本在运行结束后从 Harbor session 精确恢复，因此不需要修改带任务前缀的 LLM 地址。

恢复版导出器的适用场景、证据要求和不可恢复边界见[导出 ShareGPT SFT 数据](README_04_导出_ShareGPT.md#恢复版的证据和边界)。

## 固定导出入口

```bash
python run_canonical_export.py --verify-only
python run_canonical_export.py \
  --input-dir ./dataset_output/run_001 \
  --output-dir ./sharegpt_output/run_001
```

整理前使用 `finalize.py`，输入仍为原代理 capture 和对应 CC 原生会话。导出输出目录须不存在或为空。
固定参数与行为见 [canonical 导出说明](README_canonical_export.md)。

该包的上下文策略只在 CC 压缩摘要重启处切段，以每段最后一轮请求历史和响应生成样本；
被后续历史移除的早期内容不保证单独保留。含图段会裁到首图之前，裁剪数量写入运行报告。
嵌入式 system 消息保留相对位置；成功合并 compact 后，历史 assistant 参与训练。

`python -m unittest discover -s tests -v`：100 项通过，验收范围见 [tests/ACCEPTANCE.md](tests/ACCEPTANCE.md)。
导出器及采集核心文件保持工具包原始字节；修改四个被锁定依赖后，canonical 入口会拒绝运行，
更新代码时须同步校验清单和入口指纹。图片裁剪由 canonical 入口安装，直接调用其它导出器的行为见各自文档。

已用 Claude Code 2.1.322 和配置的 Sonnet 服务完成真实 CLI 验收：对话、Read/Write/Edit、
原生手动 compact、压缩后记忆与工具操作；12条 HTTP 请求全部关联，导出3条样本。
实测修复了前导 analysis 中引用 summary 标签导致摘要关联失败的问题，详见
[验收说明](tests/ACCEPTANCE.md)。原始日志和轨迹保留在测试者本地。

2026-11-12 重新核对官方最新版本仍为 2.1.322，并在修复后的 `6e869d4` 上用独立新会话复测通过：
原生手动 compact 计数 11228→3339，压缩后记忆及工具操作正常，12/12 条请求关联，导出3条样本。
