from __future__ import annotations

"""整理 proc_n 任务轨迹，并按每个任务的 result.json 评分拆分成功与失败。

输入目录布局：

    <proc_n>/
    ├── <group>/
    │   └── <task_id>/
    │       ├── logs/projects/**/*.jsonl
    │       └── result.json
    └── run.log

只有同时包含 session JSONL 和合法 result.json 的完整任务目录才会导出。
HTTP capture 与 session 的关联、round 排序以及主 agent / agent-acompact
镜像去重均复用 finalize.py。
"""

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from capture_core import write_json
from finalize import SessionLocation, finalize_dataset


# 与 finalize-node-split.py 一致，保留 node 导出的全部请求和响应字段。
FINAL_REQUEST_PARTS = [
    "schema_version",
    "capture_id",
    "association",
    "transport",
    "body",
    "provenance",
]

FINAL_RESPONSE_PARTS = [
    "schema_version",
    "capture_id",
    "association",
    "transport",
    "message",
    "sse_events",
    "body",
    "state",
    "provenance",
]


CORRECT = "correct"
INCORRECT = "incorrect"


@dataclass(frozen=True)
class ProcTask:
    task_id: str
    task_dir: Path
    session_files: tuple[Path, ...]
    classification: str


def classify_result_json(result_path: Path) -> str:
    """读取 checks[*].score；全部为 1.0 才正确，任一 0.0 即错误。"""

    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"无法读取 result.json: {result_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"result.json 不是有效 JSON: {result_path}: {exc}") from exc

    if not isinstance(result, dict):
        raise ValueError(f"result.json 顶层必须是 object: {result_path}")

    checks = result.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError(f"result.json 缺少非空 checks 数组: {result_path}")

    scores: list[float] = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict) or "score" not in check:
            raise ValueError(
                f"result.json 的 checks[{index}] 缺少 score: {result_path}"
            )
        score = check["score"]
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError(
                f"result.json 的 checks[{index}].score 必须是 1.0 或 0.0: "
                f"{result_path}"
            )
        if score not in (0.0, 1.0):
            raise ValueError(
                f"result.json 的 checks[{index}].score 必须是 1.0 或 0.0，"
                f"实际为 {score!r}: {result_path}"
            )
        scores.append(float(score))

    return CORRECT if all(score == 1.0 for score in scores) else INCORRECT


def discover_proc_tasks(
    proc_root: Path,
) -> tuple[list[ProcTask], list[dict[str, object]], int]:
    """发现 <proc_n>/<group>/<task_id>，跳过缺少轨迹或有效结果的目录。"""

    if not proc_root.is_dir():
        raise NotADirectoryError(f"--proc-dir 不是目录: {proc_root}")

    complete_tasks: list[ProcTask] = []
    skipped_tasks: list[dict[str, object]] = []
    discovered_task_directories = 0

    group_dirs = sorted(path for path in proc_root.iterdir() if path.is_dir())
    for group_dir in group_dirs:
        task_dirs = sorted(path for path in group_dir.iterdir() if path.is_dir())
        for task_dir in task_dirs:
            discovered_task_directories += 1
            projects_root = task_dir / "logs" / "projects"
            session_files = tuple(
                sorted(projects_root.rglob("*.jsonl"))
                if projects_root.is_dir()
                else []
            )
            result_path = task_dir / "result.json"
            problems: list[str] = []
            classification: Optional[str] = None

            if not session_files:
                problems.append("missing_session_jsonl")
            if not result_path.is_file():
                problems.append("missing_result_json")
            else:
                try:
                    classification = classify_result_json(result_path)
                except ValueError as exc:
                    problems.append("invalid_result_json")
                    result_error = str(exc)

            if problems:
                skipped: dict[str, object] = {
                    "task_id": task_dir.name,
                    "task_dir": (
                        task_dir.resolve()
                        .relative_to(proc_root.resolve())
                        .as_posix()
                    ),
                    "problems": problems,
                }
                if "invalid_result_json" in problems:
                    skipped["result_error"] = result_error
                skipped_tasks.append(skipped)
                continue

            assert classification is not None
            complete_tasks.append(
                ProcTask(
                    task_id=task_dir.name,
                    task_dir=task_dir.resolve(),
                    session_files=session_files,
                    classification=classification,
                )
            )

    task_paths_by_id: dict[str, Path] = {}
    for task in complete_tasks:
        previous = task_paths_by_id.setdefault(task.task_id, task.task_dir)
        if previous != task.task_dir:
            raise ValueError(
                "完整任务目录名重复，会覆盖同一输出 task_id: "
                f"{task.task_id}: {previous}, {task.task_dir}"
            )

    return complete_tasks, skipped_tasks, discovered_task_directories


def proc_task_context(path: Path, proc_root: Path) -> tuple[str, Path]:
    """从 <proc_n>/<group>/<task_id>/logs/projects/**.jsonl 提取任务信息。"""

    try:
        relative = path.resolve().relative_to(proc_root.resolve())
    except ValueError as exc:
        raise ValueError(f"session 不在 proc 目录内: {path}") from exc

    parts = relative.parts
    if len(parts) < 5 or tuple(parts[2:4]) != ("logs", "projects"):
        raise ValueError(
            "session 路径不符合 "
            "<proc_n>/<group>/<task_id>/logs/projects/**.jsonl: "
            f"{path}"
        )
    task_id = parts[1]
    return task_id, proc_root / parts[0] / task_id


def make_successful_location_filter(
    successful_task_dirs: set[Path],
) -> Callable[[SessionLocation], bool]:
    """为 --only-successful 只保留 score=1.0 的完整任务。"""

    return lambda location: Path(location.task_dir).resolve() in successful_task_dirs


def make_result_output_group_resolver(
    successful_task_dirs: set[Path],
) -> Callable[[SessionLocation], str]:
    """把 score=1.0 写入 successful，其余完整任务写入 failed。"""

    return lambda location: (
        "successful/tasks"
        if Path(location.task_dir).resolve() in successful_task_dirs
        else "failed/tasks"
    )


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "按 proc_n/<group>/<task_id> 目录关联采集数据，并输出 "
            "successful/tasks 与 failed/tasks"
        )
    )
    parser.add_argument(
        "--capture-dir",
        type=Path,
        required=True,
        help="proxy.py 的 --log-dir，或其 raw 子目录",
    )
    parser.add_argument(
        "--proc-dir",
        "--harbor-run-dir",
        dest="proc_dir",
        type=Path,
        required=True,
        help=(
            "单个 proc_n 目录；任务位于 <group>/<task_id>，轨迹位于 "
            "logs/projects，判定结果为同一任务目录内的 result.json"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="最终数据集目录；必须不存在或为空",
    )
    parser.add_argument(
        "--only-successful",
        action="store_true",
        help="只导出所有 checks[*].score 均为 1.0 的完整任务",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    complete_tasks, skipped_tasks, discovered_task_directories = (
        discover_proc_tasks(args.proc_dir)
    )
    session_files = [
        session_file
        for task in complete_tasks
        for session_file in task.session_files
    ]
    successful_task_dirs = {
        task.task_dir
        for task in complete_tasks
        if task.classification == CORRECT
    }
    classifications = Counter(task.classification for task in complete_tasks)
    skipped_problem_counts = Counter(
        problem
        for task in skipped_tasks
        for problem in task["problems"]
        if isinstance(problem, str)
    )
    location_filter = (
        make_successful_location_filter(successful_task_dirs)
        if args.only_successful
        else None
    )

    report = finalize_dataset(
        capture_root=args.capture_dir,
        harbor_root=args.proc_dir,
        output_root=args.output_dir,
        session_files=session_files,
        task_context_resolver=proc_task_context,
        location_filter=location_filter,
        task_output_group_resolver=make_result_output_group_resolver(
            successful_task_dirs
        ),
        request_output_parts=FINAL_REQUEST_PARTS,
        response_output_parts=FINAL_RESPONSE_PARTS,
    )
    report.update(
        {
            "harbor_layout": (
                "proc_n/<group>/<task_id>/logs/projects/**/*.jsonl + result.json"
            ),
            "output_layout": {
                "successful": "successful/tasks",
                "failed": "failed/tasks",
            },
            "full_node_fields": True,
            "only_successful": args.only_successful,
            "discovered_task_directories": discovered_task_directories,
            "complete_task_directories": len(complete_tasks),
            "skipped_incomplete_task_directories": len(skipped_tasks),
            "skipped_incomplete_problem_counts": dict(
                sorted(skipped_problem_counts.items())
            ),
            "skipped_incomplete_tasks": skipped_tasks,
            "discovered_session_files": len(session_files),
            "result_score_classifications": dict(sorted(classifications.items())),
            "result_task_directory_catalog": {
                "successful": len(successful_task_dirs),
                "failed": len(complete_tasks) - len(successful_task_dirs),
            },
            "exported_trajectory_tasks": {
                "successful": report.get("task_output_groups", {}).get(
                    "successful/tasks", 0
                ),
                "failed": report.get("task_output_groups", {}).get(
                    "failed/tasks", 0
                ),
            },
        }
    )

    (args.output_dir / "successful" / "tasks").mkdir(parents=True, exist_ok=True)
    (args.output_dir / "failed" / "tasks").mkdir(parents=True, exist_ok=True)
    write_json(args.output_dir / "finalization_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
