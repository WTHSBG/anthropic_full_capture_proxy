# CC 轨迹整理及导出教程

## 从采集开始

启动 proxy.py 并将 Claude Code 的 ANTHROPIC_BASE_URL 指向代理。认证和模型沿用原配置，
代理的 --upstream-url 指向原模型服务。安装步骤见 [安装并启动代理](README_01_安装并启动代理.md)。

客户端结束后，保留同一轮运行的 capture 目录及 CC 原生会话，再整理：

```bash
python finalize.py \
  --capture-dir ./capture_logs/run_001 \
  --harbor-run-dir /path/to/harbor-run/tasks \
  --output-dir ./dataset_output/run_001
```

新式 compact 的 system 尾部消息和 anchorUuid/attachment 关联由包内 finalize.py 处理。
对应 session、时间、摘要正文等恢复证据仍须完整；关联不足的记录不会被强行补入 task。

## 从已整理的数据开始

input-dir 需要包含 tasks/<task>/<agent>/round_*/request.json 与 response.json。
先核验指纹，再导出到不存在或为空的目录：

```bash
python run_canonical_export.py --verify-only
python run_canonical_export.py \
  --input-dir ./dataset_output/run_001 \
  --output-dir ./sharegpt_output/run_001
```

如果只运行离线导出，不需要安装代理依赖。输出是 sharegpt.jsonl 和各条 JSON，
记录使用 messages/role/content，思考在 reasoning_content 中，工具采用 HF tool-calling 双写格式。

## 检查输出

查看输出目录的 export_errors.json、recovery_report.json、optimization_report.json，
以及旁边的 run_001_canonical_run_report.json。报告记录恢复、compact 合并、截断目标处理和图片裁剪数量。
输入有图片时，文本前缀之外的部分可能被裁掉；切分、mask 和失败分类的完整规则见
[canonical 导出说明](README_canonical_export.md)。

指纹不符时核对被修改的依赖，不要删除门禁后继续导出；输出非空时使用新目录。
保留原始 capture 和 CC 会话以便重新整理。
