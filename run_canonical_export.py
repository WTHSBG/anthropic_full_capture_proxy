#!/usr/bin/env python3
"""规范化 ShareGPT 导出运行器（唯一入口；今后所有导出一律使用本脚本）。

用法:
    python3 run_canonical_export.py --input-dir <含 tasks/<task>/<agent>/round_* 的目录> \
                                    --output-dir <空输出目录>
    python3 run_canonical_export.py --verify-only   # 仅校验脚本指纹

固定行为（不得改动，改动即不再是规范化导出）:
  1. 指纹门禁：目录内 4 个脚本必须与 SHA256SUMS 完全一致，否则拒绝导出。
  2. 导出器：export_sharegpt_recovered_merged（GitHub main 299e4310…）。
  3. base：export_sharegpt.py（本地 CC 2.x 兼容版 a2d28ee5…，含 4 个修复：
     is_compaction_restart 压缩边界切分 / 嵌入式 system 原位独立保留（2026-11-12
     修复：不再 separate 合并进首条 system）/ clean_system_text+TOTAL_TOKENS_PATTERN /
     segment_contains_image）。
     在本数据上已实证：其切分与 GitHub 连续性判据逐对等价（161 agent / 139 边界
     100% 一致），且纯 GitHub base 会每轮一段并崩溃（0 条产出）。
  4. image 段处理（2026-11-05 改造）：含 image 的段截断保留首个 image 轮之前的
     文本前缀（原为整段丢弃；B 组 L130/L129 裁置，备份
     run_canonical_export.py.bak-20261105-imageprefix）。
  5. 标准参数：reasoning_mode="separate", standard_structure=True,
     mask_compact_assistant_history=True, merge_compact_trajectories=True,
     task_group="auto"。

产出（output-dir 内）: <task>/<agent>_<seg>.json、sharegpt.jsonl、
export_report.json、recovery_report.json、optimization_report.json、
export_errors.json；运行器自身报告写入 --report（默认输出目录旁
<name>_canonical_run_report.json）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 指纹（与 SHA256SUMS 一致；双份冗余以防文件被同时篡改）
PINNED = {
    "export_sharegpt.py":
        "5fbe1d3783d0108b09e26a21043c66519c5cc0c86e2efa7d61917b03cc76c45a",
    "export_sharegpt_recovered_merged.py":
        "299e4310be64f246b54c02f27a60ff902ff6b00350face9a58ebb890c2a2c82b",
    "capture_core.py":
        "511e0c022337801cde609a98d6a19e78a299b2bac884368b88be9246c1ba409f",
    "export_sharegpt_recovered.py":
        "8abda3c79bae61346397c309cfc54d1a2fc2f406f6260c63671800581ace4d98",
}

STANDARD_PARAMS = {
    "reasoning_mode": "separate",
    "standard_structure": True,
    "mask_compact_assistant_history": True,
    "merge_compact_trajectories": True,
    "task_group": "auto",
}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_fingerprints() -> list[str]:
    problems = []
    for name, pinned in PINNED.items():
        p = HERE / name
        if not p.is_file():
            problems.append(f"缺失 {name}")
            continue
        actual = sha256_of(p)
        if actual != pinned:
            problems.append(f"{name}: 指纹不符 expected={pinned[:16]}… actual={actual[:16]}…")
    sums = HERE / "SHA256SUMS"
    if not sums.is_file():
        problems.append("缺失 SHA256SUMS")
    else:
        expected_lines = {f"{pinned}  {name}" for name, pinned in PINNED.items()}
        actual_lines = {l.strip() for l in sums.read_text(encoding="utf-8").splitlines() if l.strip()}
        if actual_lines != expected_lines:
            problems.append("SHA256SUMS 与内置指纹不一致")
    return problems


def install_image_skip():
    """image 段处理：截断到首个 image 轮之前，保留文本前缀（不再整段丢弃）。

    2026-11-05 改造（B 组 L130/L129 裁置）：原行为是"含 image 的上下文段整段
    丢弃"，对单段贯穿全程的 agent（无压缩重启）等于丢弃整个 agent。因 CC 上下文
    单调增长，image block 一旦进入历史就会出现在之后每一轮的请求里，唯一无损
    可保留的是首个 image 轮之前的文本前缀——现将其保留为独立段；首个 image 轮
    及之后的部分继续丢弃（文本转换遇到 image 仍会崩溃）。

    统计只在每个 agent 轮序列首次 split 时累计（build_task 会对同一序列调用
    split 两次：original_context_splits 统计 + 实际切分），避免重复计数。
    """
    import export_sharegpt as base

    original_split = base.split_rounds_by_context
    stats = {"segments_total": 0, "segments_image_skipped": 0,
             "rounds_in_image_segments": 0,
             "segments_image_truncated": 0,
             "rounds_kept_as_prefix": 0,
             "rounds_dropped_after_image": 0}
    counted_keys: set = set()

    def round_contains_image(round_record) -> bool:
        request = round_record.request_body
        return any(
            base.contains_image_block(value)
            for value in (
                request.get("system"),
                request.get("messages"),
                round_record.response_message.get("content"),
            )
        )

    def split_without_image_segments(rounds):
        key = tuple(r.round_dir for r in rounds)
        segments = original_split(rounds)
        first_time = key not in counted_keys
        kept = []
        for segment in segments:
            if not base.segment_contains_image(segment):
                kept.append(segment)
                continue
            # 该段含 image：截断到首个 image 轮之前，保留文本前缀
            first_image_idx = next(
                (i for i, r in enumerate(segment) if round_contains_image(r)),
                len(segment),
            )
            if first_time:
                stats["segments_image_truncated"] += 1
                stats["rounds_kept_as_prefix"] += first_image_idx
                stats["rounds_dropped_after_image"] += len(segment) - first_image_idx
            if first_image_idx > 0:
                kept.append(segment[:first_image_idx])
            else:
                if first_time:
                    stats["segments_image_skipped"] += 1
                    stats["rounds_in_image_segments"] += len(segment)
                continue
        if first_time:
            stats["segments_total"] += len(segments)
            counted_keys.add(key)
        return kept

    base.split_rounds_by_context = split_without_image_segments
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="规范化 ShareGPT 导出（勿改动行为）")
    parser.add_argument("--input-dir", type=Path, help="含 tasks/<task>/<agent>/round_* 的目录")
    parser.add_argument("--output-dir", type=Path, help="空输出目录（不存在或为空）")
    parser.add_argument("--report", type=Path, default=None,
                        help="运行器报告路径（默认输出目录旁 <name>_canonical_run_report.json）")
    parser.add_argument("--verify-only", action="store_true", help="仅校验脚本指纹后退出")
    args = parser.parse_args()

    problems = verify_fingerprints()
    if problems:
        print("[拒绝] 规范化脚本指纹校验失败：", file=sys.stderr)
        for p in problems:
            print("  -", p, file=sys.stderr)
        return 2
    print("[OK] 指纹校验通过：4 个脚本与 SHA256SUMS 一致")

    if args.verify_only:
        return 0
    if not args.input_dir or not args.output_dir:
        parser.error("--input-dir 与 --output-dir 必填（或使用 --verify-only）")
    if args.input_dir.resolve() == args.output_dir.resolve():
        parser.error("输入输出目录不能相同")

    # 本目录脚本优先于任何其他同名模块
    sys.path.insert(0, str(HERE))
    import export_sharegpt_recovered_merged as merged

    stats = install_image_skip()
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise SystemExit(f"输出目录非空: {args.output_dir}")

    report = merged.export_sharegpt_recovered_merged(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        **STANDARD_PARAMS,
    )
    report["image_skip"] = stats
    report["canonical_runner"] = {
        "script": str(Path(__file__).resolve()),
        "fingerprints": PINNED,
        "params": STANDARD_PARAMS,
    }

    report_path = args.report or args.output_dir.parent / (
        args.output_dir.name + "_canonical_run_report.json")
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    keep = ("tasks", "agents", "rounds", "sharegpt_files", "context_splits",
            "original_context_splits", "merged_compacts", "discarded_truncated_rounds",
            "masked_truncated_history", "recovered_rounds", "skipped_agents",
            "masked_assistant_messages")
    print(json.dumps({k: report[k] for k in keep}, ensure_ascii=False, indent=2))
    print(f"\n报告: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
