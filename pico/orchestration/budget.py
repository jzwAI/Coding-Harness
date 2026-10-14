"""BudgetLedger：多 agent 协作的全局预算账本（线程安全）。

多个子 agent 并行时，单个 agent 的 max_steps 管不住总成本。这里给整次协作
设全局上限：总步数、总估算 prompt token、任务数、重试次数。每个子 agent
开始前先申请任务额度，结束后登记实际消耗；额度耗尽后不再放行新任务。
"""

from __future__ import annotations

import threading

DEFAULT_MAX_TOTAL_STEPS = 120
DEFAULT_MAX_TOTAL_PROMPT_TOKENS = 2_000_000
DEFAULT_MAX_TASKS = 24
DEFAULT_MAX_ATTEMPTS = 5


class BudgetExhausted(Exception):
    """预算耗尽时由 try_start_task 抛出，orchestrator 捕获后停止调度。"""


class BudgetLedger:
    def __init__(
        self,
        max_total_steps=DEFAULT_MAX_TOTAL_STEPS,
        max_total_prompt_tokens=DEFAULT_MAX_TOTAL_PROMPT_TOKENS,
        max_tasks=DEFAULT_MAX_TASKS,
        max_attempts=DEFAULT_MAX_ATTEMPTS,
    ):
        self._lock = threading.Lock()
        self.max_total_steps = int(max_total_steps)
        self.max_total_prompt_tokens = int(max_total_prompt_tokens)
        self.max_tasks = int(max_tasks)
        self.max_attempts = int(max_attempts)
        self.used_steps = 0
        self.used_prompt_tokens = 0
        self.started_tasks = 0        # 累计启动过的任务尝试次数

    def try_start_task(self):
        """申请启动一次任务尝试。返回 (ok, reason)。"""
        with self._lock:
            if self.started_tasks >= self.max_tasks:
                return False, f"max_tasks ({self.max_tasks}) reached"
            if self.used_steps >= self.max_total_steps:
                return False, f"max_total_steps ({self.max_total_steps}) reached"
            if self.used_prompt_tokens >= self.max_total_prompt_tokens:
                return False, f"max_total_prompt_tokens ({self.max_total_prompt_tokens}) reached"
            self.started_tasks += 1
            return True, ""

    def record_usage(self, steps=0, prompt_tokens=0):
        with self._lock:
            self.used_steps += int(steps or 0)
            self.used_prompt_tokens += int(prompt_tokens or 0)

    def exhausted(self):
        with self._lock:
            return (
                self.used_steps >= self.max_total_steps
                or self.used_prompt_tokens >= self.max_total_prompt_tokens
                or self.started_tasks >= self.max_tasks
            )

    def snapshot(self):
        with self._lock:
            return {
                "used_steps": self.used_steps,
                "max_total_steps": self.max_total_steps,
                "used_prompt_tokens": self.used_prompt_tokens,
                "max_total_prompt_tokens": self.max_total_prompt_tokens,
                "started_tasks": self.started_tasks,
                "max_tasks": self.max_tasks,
                "max_attempts": self.max_attempts,
            }
