"""ApprovalBroker：把多个子 agent 的审批请求串行化。

并行时若多个子 agent 同时请求审批，终端提示会混在一起。这里用一把锁让审批
请求一个一个来，并在提示里标注来源（哪个任务 / 哪个角色）。auto / never 策略
不需要人工介入，直接放行或拒绝，不排队。
"""

from __future__ import annotations

import json
import threading


class ApprovalBroker:
    def __init__(self, policy="auto", prompt_func=None):
        self.policy = policy
        self._lock = threading.Lock()
        self._prompt = prompt_func or input

    def request(self, source, name, args):
        """返回 True 表示批准。source 形如 't2/worker'。"""
        if self.policy == "auto":
            return True
        if self.policy == "never":
            return False
        # policy == "ask"：串行提示，带上来源标注
        with self._lock:
            try:
                answer = self._prompt(f"[{source}] approve {name} {json.dumps(args, ensure_ascii=True)}? [y/N] ")
            except EOFError:
                return False
            return str(answer).strip().lower() in {"y", "yes"}
