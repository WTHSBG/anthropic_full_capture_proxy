# 本分支验收

本分支从原仓库 main 的 b4e0874 构建，四个被固定入口锁定的导出/采集依赖保持工具包原始字节；finalize.py 追加实测摘要关联修复。
来源及指纹见 EXPORT_KIT_SOURCE.json。

`python -m unittest discover -s tests -v`：100 项通过。上游原有 91 项测试中，
两个用例因工具包窗口切分策略变化而调整：歧义前缀用例改为跨真实压缩窗口构造相同最长前缀，
继续断言拒绝多义合并；截断思考用例保留样本内容检查，将无原生边界时的切分统计改为0。

新增7项验证：来源指纹、依赖/SHA256SUMS 篡改拦截、嵌入 system 原位保留、
图片前缀裁剪、原生 compact 标记分段、非压缩历史重置按末轮代表、
新式 CC compact finalize 后通过 canonical CLI 导出及历史 mask。输入文件哈希保持一致。

这些测试使用构造的请求、SSE 和 CC 会话；网络用例只连接本机或使用模拟上游。
没有使用真实模型凭据，也没有据此宣称新版 CC CLI 的真实服务已重新验收。
导出切分与裁剪会改变保留样本，请按 README_canonical_export.md 检查报告。

## Claude Code 2.1.322 真实验收

通过临时安装的 Claude Code 2.1.322，使用本机配置的 claude-sonnet-5 服务，
以官方 CLI print/resume 模式驱动同一原生会话。使用独立配置目录、safe/restricted 模式、
Read/Write/Edit 三个内置工具及原生 /compact；没有加载用户插件或自定义 MCP。

对话及记忆检查通过；压缩前 Read→Write→Read，压缩后 Read→Edit→Read，
文件值及计算结果正确。原生记录显示 manual compact 的 token 计数从10749降到3690，
生成 compact_boundary 和摘要，anchorUuid 指向摘要 uuid；压缩后无工具读取即复述初始记忆标记。

最初12条请求中11条关联，1条摘要未关联：模型在 analysis 中引用 summary 标签，
旧正则误选摘要起点。保留失败产物后修复提取，在同一批未修改的真实原始记录上重新整理，
12条全部匹配，unmatched/conflicts 均为0，compact 恢复1条。增加两条回归：
分析内标签不干扰完整摘要、仅分析内的示例摘要不恢复。

固定入口导出3条样本，无跳过agent、无图片裁剪，6个工具调用ID和所有结果内容均有真实HTTP见证。
CC 对 Edit 的重放参数补了默认 replace_all=false，导出遵循该有证据的重放值；其余输入参数一致。
独立compact样本有5条历史 masked_assistant，最后摘要保留assistant；合并compact数为0。
整理和导出前后原始HTTP、原生会话文件哈希一致，未检出明文凭据，用户配置和原CLI安装未修改。

本次覆盖手动compact和CLI非交互恢复，不覆盖终端按键UI、自动compact、子agent或所有模型。
真实日志和导出仅保存在测试者本地，不提交公开仓库。
