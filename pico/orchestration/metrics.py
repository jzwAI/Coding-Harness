"""多 agent 协作的汇总指标（纯函数，便于测试）。

从一块跑完的 TaskBoard 算出任务数、各状态计数、总步数/估算 token、冲突与越界次数、
以及加速比（各任务耗时之和 ÷ 实际墙钟耗时）。写进父 report 的 multi_agent 字段。
"""

from __future__ import annotations


def summarize_board(board, wall_clock_ms=None, concurrency_peak=None, merge_conflicts=0):
    tasks = board.tasks
    status_counts = {}
    total_steps = 0
    total_tokens = 0
    total_task_ms = 0
    scope_violations = 0
    retries = 0
    for task in tasks:
        status_counts[task["status"]] = status_counts.get(task["status"], 0) + 1
        usage = task.get("usage") or {}
        total_steps += int(usage.get("steps", 0) or 0)
        total_tokens += int(usage.get("prompt_tokens_est", 0) or 0)
        # 所有尝试的耗时都计入"任务耗时之和"
        for attempt in task.get("attempts_history", []):
            total_task_ms += int((attempt.get("usage") or {}).get("duration_ms", 0) or 0)
        retries += max(0, len(task.get("attempts_history", [])) - 1)
        if task["status"] == "scope_violation":
            scope_violations += 1

    speedup = None
    if wall_clock_ms:
        speedup = round(total_task_ms / wall_clock_ms, 3) if wall_clock_ms > 0 else None

    return {
        "task_count": len(tasks),
        "status_counts": status_counts,
        "total_steps": total_steps,
        "total_prompt_tokens_est": total_tokens,
        "sum_task_duration_ms": total_task_ms,
        "wall_clock_ms": wall_clock_ms,
        "speedup": speedup,
        "concurrency_peak": concurrency_peak,
        "merge_conflicts": merge_conflicts,
        "scope_violations": scope_violations,
        "retries": retries,
    }
