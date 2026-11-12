# 本分支验收

本分支从原仓库 main 的 b4e0874 构建，导出相关代码保持与 sharegpt-export-kit(1) 原文件逐字节一致。
来源及指纹见 EXPORT_KIT_SOURCE.json。

`python -m unittest discover -s tests -v`：98 项通过。上游原有 91 项测试中，
两个用例因工具包窗口切分策略变化而调整：歧义前缀用例改为跨真实压缩窗口构造相同最长前缀，
继续断言拒绝多义合并；截断思考用例保留样本内容检查，将无原生边界时的切分统计改为0。

新增7项验证：来源指纹、依赖/SHA256SUMS 篡改拦截、嵌入 system 原位保留、
图片前缀裁剪、原生 compact 标记分段、非压缩历史重置按末轮代表、
新式 CC compact finalize 后通过 canonical CLI 导出及历史 mask。输入文件哈希保持一致。

这些测试使用构造的请求、SSE 和 CC 会话；网络用例只连接本机或使用模拟上游。
没有使用真实模型凭据，也没有据此宣称新版 CC CLI 的真实服务已重新验收。
导出切分与裁剪会改变保留样本，请按 README_canonical_export.md 检查报告。
