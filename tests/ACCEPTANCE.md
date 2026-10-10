# Kimi / Hermes 验收说明

本适配经过自动化测试、真实客户端与本地确定性服务验收，以及显式授权的真实模型
服务验收。原始会话和导出轨迹保留在测试者本地，不放入公开仓库。

## 自动化测试

`python -m unittest discover -s tests -v`：77 项通过，包含原有 Claude Code / Harbor
测试和新增协议、会话适配、compact、透明转发及采集故障测试。无需模型凭据或外网。
真实 TCP 用例仅连接本机。

## 真实服务与客户端

受测版本：Kimi Code 2.0.0；Hermes commit
`dce1e9b37581dd62e480a9064dc04a709c2940d3`。

- Anthropic Messages / OpenAI Chat Completions，流式和非流式，三个模型共 12 个用例。
  本地观察服务对照同一次上游交易：请求/响应字节、认证、路径、状态与受测响应头一致。
  DeepSeek/K3 正常生成；GLM 直连和转发均为上游 502，只确认错误透明转发。
- Kimi Anthropic、Hermes custom Chat provider 分别在 DeepSeek/K3 执行真实文件工具。
  四组、8 条导出样本通过工具结果、响应 ID/历史及可见 reasoning 验证。
- 最终代码重新导出上述四组，与原先验证通过的 8 条样本逐条 JSON 一致。

## 原生 compact

compact 实测使用 DeepSeek；五组全部通过：

| 场景 | 主模型轮次 | 摘要轮次 | 恢复导出样本 |
| --- | --- | --- | --- |
| Kimi ACP 手动 `/compact` | 4 | 1 | 5 |
| Kimi 自动压缩后再手动 `/compact` | 5 | 2 | 7 |
| Hermes 手动 `/compress`，原会话内压缩 | 6 | 1 | 7 |
| Hermes 手动 `/compress`，后继会话 | 6 | 1 | 7 |
| Hermes 自动 preflight 压缩 | 6 | 1 | 7 |

逐组核对摘要实际应用、压缩后文件工具、旧标记复述、主/摘要轮次关联、摘要正文、
可见 reasoning、工具 ID/名称/参数和工具结果。严格与恢复导出均通过；导出前后原始
HTTP 文件哈希一致。自动触发使用调整后的上下文大小/阈值验证原生逻辑。

Hermes 还验证了“摘要使上下文增长”的拒绝路径：未归档/丢弃消息或伪造应用边界，
下一轮工具和旧标记正常；被拒绝的摘要留在 unmatched。客户端占位 reasoning 只用于
证据投影，不覆盖模型输出；压缩样本历史 assistant 的 mask 保持原有规则。

Hermes 通过真实 `HermesCLI.chat` 和 `/compress` 原生处理器驱动，未覆盖终端按键。
Responses 的真实服务、原生子 agent、长时间负载、GLM/K3 compact 及所有客户端版本/
供应商的异常组合尚未实测。未知改写或歧义保持 unmatched/conflicts。

复现脚本见 [接入文档](../README_07_Kimi_Code与Hermes.md#复现验收)。
