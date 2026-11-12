# canonical 导出说明

本仓库根目录集成 sharegpt-export-kit(1) 的完整固定导出环境，代码来源和指纹见
[EXPORT_KIT_SOURCE.json](EXPORT_KIT_SOURCE.json)。原包 canonical-export/ 下的代码已平铺到根目录；
finalize.py 也已合入根目录。使用命令无需依赖原压缩包路径。

## 依赖及指纹

离线整理和导出仅使用 Python 标准库。代理和其本机测试依赖仍见 requirements.txt。
本次验证环境为 Python 3.13。

run_canonical_export.py 校验四个依赖：export_sharegpt.py、export_sharegpt_recovered.py、
export_sharegpt_recovered_merged.py、capture_core.py；同时检查 SHA256SUMS 与入口内置清单一致。
入口自身及 finalize.py 不在运行时指纹门禁中，完整来源指纹另见 EXPORT_KIT_SOURCE.json。

```bash
python run_canonical_export.py --verify-only
python run_canonical_export.py --input-dir ./dataset_output/run_001 --output-dir ./sharegpt_output/run_001
```

## 固定参数

- reasoning_mode=separate；思考保存到 reasoning_content。
- standard_structure=True；保留 HF tool-calling 嵌套字段与扁平别名。
- mask_compact_assistant_history=True；未合并的独立 compact 样本历史 assistant 使用 masked_assistant。
- merge_compact_trajectories=True；成功合并 compact 后，历史 assistant 参与训练，截断历史的 mask 仍保留。
- task_group=auto；超限失败分类规则见合并导出器说明。

## 行为

- 仅在 CC 英文压缩摘要标记与消息数下降同时出现时切分上下文窗口。
  每段以末轮完整历史与响应代表，历史被替换或模型切换不会额外分段；可能不再单独保留早期答案。
- partial 响应需要相邻重试或后续请求等证据恢复；无法恢复的 agent 记录在错误报告中。
- system 中清理 billing header 和 total_tokens；嵌入 system 保留原位置，清理后可为空。
- canonical 入口对已加载的含图上下文窗口保留首图之前的文本前缀，丢弃首图所在轮及之后的内容，
  通过 image_skip 报告裁剪数量。此功能是文本导出的裁剪策略，不是多模态导出。
- 输出 messages/role/content 与工具字段，而非 conversations/from/value。
- 原始输入文件不修改，输出目录须不存在或为空；重复运行应选新的输出目录。

## 输出

输出目录包含各 task 的 JSON、sharegpt.jsonl、export_report.json、export_errors.json、
recovery_report.json 和 optimization_report.json。按规则隔离的失败任务进入 limit_exceeded/。
入口运行报告默认位于输出目录旁的 `<输出目录名>_canonical_run_report.json`，包含 image_skip、参数及指纹；
可用 --report 指定。

本分支从原仓库 main 构建，面向 CC/Harbor，不含独立多客户端分支中的 Kimi/Hermes 或自动归档扩展。
本仓库验证范围见 [tests/ACCEPTANCE.md](tests/ACCEPTANCE.md)。
