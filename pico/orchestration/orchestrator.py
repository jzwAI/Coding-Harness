"""Orchestrator：把一个大任务拆给多个受限子 agent，并行执行并收集结果。

能力分层（都在这个类里，按设计文档的 Phase 2/3/4）：
- Phase 2：wave 式并行调度（线程池）、全局预算账本、审批串行化、取消。
- Phase 3：worker 在独立 git worktree 里隔离写代码；Integrator 合并分支、
  检查冲突与越界，验证通过后再应用回主工作区。非 git 仓库退回串行、直接写主工作区。
- Phase 4：best-of-N 变体与择优。

对父 agent（depth=0）暴露工具：plan_tasks / run_tasks / board / integrate /
apply_integration / select_variant。子 agent 拿不到这些工具，委派深度固定 <= 1。

写 board 只在主线程进行；子 agent 在工作线程里执行，结果经 future 回到主线程后
才落到 board，避免并发写坏看板。
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .approval import ApprovalBroker
from .blackboard import Blackboard, CollabHandle
from .budget import BudgetLedger
from .handoff import build_brief, parse_result
from .integrator import Integrator
from .metrics import summarize_board
from .task_board import (
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_SCOPE_VIOLATION,
    BoardError,
    BoardStore,
    TaskBoard,
    new_task,
    path_in_scope,
)
from .verify import run_all
from .worktree import WorktreeManager

READ_ONLY_ROLES_DEFAULT_CONCURRENCY = 3


class Orchestrator:
    def __init__(self, agent, roles, max_concurrency=3, budget=None):
        self.agent = agent
        self.roles = roles
        self.max_concurrency = max(1, int(max_concurrency))
        self.budget = budget or BudgetLedger()
        self.broker = ApprovalBroker(policy=agent.approval_policy)
        self.cancel_event = threading.Event()
        self._board = None
        self._store = None
        self._emit_lock = threading.Lock()
        self._git_lock = threading.Lock()  # git worktree/commit 改共享 .git，需串行
        self._wt = WorktreeManager(agent.root)
        self._git = self._wt.is_git_repo()
        self._concurrency_peak = 0
        self._merge_conflicts = 0
        self._blackboard = None   # 懒加载（需要 run 目录）

    # ---------- board 存取 ----------

    def _run_dir(self):
        task_state = getattr(self.agent, "current_task_state", None)
        if task_state is None:
            raise BoardError("orchestration tools are only available while a run is active")
        return self.agent.run_store.run_dir(task_state)

    def _ensure_store(self):
        if self._store is None:
            self._store = BoardStore(self._run_dir())
        return self._store

    def _load_or_none(self):
        if self._board is not None:
            return self._board
        store = self._ensure_store()
        if store.exists():
            self._board = store.load()
            self._board.recover_running()
        return self._board

    def _save(self):
        if self._board is not None:
            self._ensure_store().save(self._board)

    def blackboard(self):
        if self._blackboard is None:
            self._blackboard = Blackboard(store_path=self._run_dir() / "blackboard.json")
        return self._blackboard

    # ---------- 工具 1：plan_tasks ----------

    def plan_tasks(self, tasks_raw):
        if not isinstance(tasks_raw, list) or not tasks_raw:
            return "error: plan_tasks requires a non-empty 'tasks' array"
        task_state = getattr(self.agent, "current_task_state", None)
        run_id = task_state.run_id if task_state else ""
        base_commit = self._wt.head_commit() if self._git else ""

        existing = self._load_or_none()
        board = existing if existing is not None else TaskBoard(run_id=run_id, base_commit=base_commit)
        if not board.base_commit and base_commit:
            board.base_commit = base_commit

        expanded = []
        for raw in tasks_raw:
            expanded.extend(self._expand_variants(raw if isinstance(raw, dict) else {}))

        by_id = {t["task_id"]: t for t in board.tasks}
        for raw in expanded:
            task = new_task(raw)
            prior = by_id.get(task["task_id"])
            if prior and prior["status"] not in ("pending", "blocked"):
                for key in ("goal", "acceptance", "verify", "inputs", "depends_on", "write_scope", "model", "max_attempts", "variant_of"):
                    prior[key] = task[key]
                if prior["status"] == "blocked":
                    prior["status"] = "pending"
            elif prior:
                prior.update({k: task[k] for k in task if k not in ("attempt", "attempts_history", "created_at")})
            else:
                board.tasks.append(task)
                by_id[task["task_id"]] = task

        try:
            board.validate(known_roles=set(self.roles.names()))
        except BoardError as exc:
            return f"error: {exc}"

        self._board = board
        self._save()
        self._emit("tasks_planned", {"task_count": len(board.tasks), "task_ids": board.ids()})
        return "planned " + str(len(board.tasks)) + " tasks:\n" + _format_compact(board.compact())

    @staticmethod
    def _expand_variants(raw):
        """best-of-N：带 variants:N 的任务展开成 N 个共享 variant_of 的候选。"""
        try:
            n = int(raw.get("variants", 1))
        except (TypeError, ValueError):
            n = 1
        if n <= 1:
            return [raw]
        base_id = str(raw.get("task_id", "")).strip()
        out = []
        for k in range(1, n + 1):
            copy = dict(raw)
            copy.pop("variants", None)
            # 分隔符必须落在合法 task_id 字符集 [a-z0-9_-] 内，不能用 "#"（会被 board 校验拒绝）。
            copy["task_id"] = f"{base_id}-v{k}"
            copy["variant_of"] = base_id
            out.append(copy)
        return out

    # ---------- 工具 2：run_tasks ----------

    def run_tasks(self, selector, mode="wave"):
        board = self._load_or_none()
        if board is None:
            return "error: no task board; call plan_tasks first"
        mode = str(mode or "wave").strip().lower()
        if mode not in ("wave", "all"):
            mode = "wave"

        explicit = None
        if selector not in (None, "", "ready"):
            explicit = [selector] if isinstance(selector, str) else list(selector)
            done = {t["task_id"] for t in board.tasks if t["status"] == STATUS_DONE}
            for tid in explicit:
                task = board.task(tid)
                if task is None:
                    return f"error: unknown task_id {tid!r}"
                if task["status"] != "pending":
                    return f"error: task {tid!r} is not pending (status={task['status']})"
                missing = [d for d in task["depends_on"] if d not in done]
                if missing:
                    return f"error: task {tid!r} has unmet dependencies: {', '.join(missing)}"

        wall_start = time.monotonic()
        ran = self._schedule(board, explicit, mode)
        wall_ms = int((time.monotonic() - wall_start) * 1000)
        self._auto_select_variants(board)
        self._save()

        if not ran:
            return "no ready tasks. board:\n" + _format_compact(board.compact())
        return self._digest(board, ran, wall_ms)

    def _digest(self, board, ran, wall_ms):
        """返回结构化进度摘要文本，供组长在波次之间决策。"""
        ran_set = set(ran)
        done, failed, blocked, scope = [], [], [], []
        for t in board.tasks:
            if t["task_id"] not in ran_set:
                continue
            if t["status"] == STATUS_DONE:
                done.append(t["task_id"])
            elif t["status"] == STATUS_FAILED:
                failed.append({"id": t["task_id"], "reason": t["stop_reason"], "error": t["error"][:160]})
            elif t["status"] == STATUS_BLOCKED:
                needs = (t.get("result") or {}).get("needs", [])
                blocked.append({"id": t["task_id"], "needs": needs})
            elif t["status"] == STATUS_SCOPE_VIOLATION:
                scope.append({"id": t["task_id"], "error": t["error"][:160]})
        ready_next = [t["task_id"] for t in board.ready_tasks()]
        recent_notes = []
        if self._blackboard is not None:
            recent_notes = [{"key": n["key"], "from": n["from_task"]} for n in self._blackboard.read(limit=5)]
        digest = {
            "ran": list(ran),
            "completed": done,
            "failed": failed,
            "blocked": blocked,
            "scope_violation": scope,
            "ready_next": ready_next,
            "budget": self.budget.snapshot(),
            "blackboard_recent": recent_notes,
            "cancelled_early": self.cancel_event.is_set(),
            "wall_ms": wall_ms,
        }
        lines = [f"ran {len(ran)} task(s) in {wall_ms}ms:"]
        lines.append(_format_compact(board.compact()))
        lines.append("")
        lines.append("progress digest: " + json.dumps(digest, ensure_ascii=False))
        if ready_next:
            lines.append(f"ready to run next: {', '.join(ready_next)} (call run_tasks again)")
        if blocked:
            lines.append("blocked tasks need answers: use answer_need(task_id, text)")
        if self.cancel_event.is_set():
            lines.append("[stopped early: " + (self._cancel_reason or "cancelled") + "]")
        return "\n".join(lines)

    _cancel_reason = ""

    def _schedule(self, board, explicit, mode="wave"):
        """调度就绪任务并行跑。

        mode="wave"：只跑一波（最多 max_concurrency 个），跑完即返回，把控制权交回组长。
        mode="all"：反复跑到无就绪任务或被取消（v1 旧行为）。
        """
        ran = []
        # 可写任务且无 git 隔离时，退回串行，避免并行写坏共享工作区。
        while True:
            if self.cancel_event.is_set():
                break
            if explicit is not None:
                wave = [board.task(t) for t in explicit if board.task(t) and board.task(t)["status"] == "pending"]
            else:
                wave = board.ready_tasks()
            if not wave:
                break

            needs_serial = (not self._git) and any(not self._role_read_only(t) for t in wave)
            concurrency = 1 if needs_serial else self.max_concurrency

            # 预算门：逐个申请任务额度
            admitted = []
            for task in wave:
                ok, reason = self.budget.try_start_task()
                if not ok:
                    self._trigger_cancel("budget_exhausted:" + reason)
                    break
                admitted.append(task)
            if not admitted:
                break

            # 主线程先标 running 再派发
            for task in admitted:
                role = self.roles.get(task["role"])
                board.mark_running(task["task_id"], child_run_id="",
                                   model_resolved=(role.model if role else task["role"]))
                self._emit("task_started", {"task_id": task["task_id"], "role": task["role"], "attempt": task["attempt"]})
            self._save()

            self._concurrency_peak = max(self._concurrency_peak, min(len(admitted), concurrency))
            outcomes = {}
            if concurrency == 1:
                for task in admitted:
                    outcomes[task["task_id"]] = self._execute_attempt(dict(task), self.roles.get(task["role"]))
            else:
                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    future_map = {
                        pool.submit(self._execute_attempt, dict(task), self.roles.get(task["role"])): task["task_id"]
                        for task in admitted
                    }
                    for future in future_map:
                        tid = future_map[future]
                        try:
                            outcomes[tid] = future.result()
                        except Exception as exc:  # noqa: BLE001
                            outcomes[tid] = {"status": STATUS_FAILED, "stop_reason": "model_error",
                                             "error": f"worker thread raised: {exc}", "result": None,
                                             "usage": {}, "child_run_id": "", "changed_files": []}

            # 主线程统一落 board
            for task in admitted:
                self._apply_outcome(board, task, outcomes.get(task["task_id"], {}))
                ran.append(task["task_id"])
            self._save()

            # wave 模式：跑完这一波就把控制权交回组长（可据进度摘要调整计划）。
            if mode == "wave":
                break
            if explicit is not None:
                break  # explicit 选择只跑它们这一波
        return ran

    def _role_read_only(self, task):
        role = self.roles.get(task["role"])
        return bool(role and role.read_only)

    def _trigger_cancel(self, reason):
        self._cancel_reason = reason
        self.cancel_event.set()
        self._emit("budget_exhausted" if reason.startswith("budget") else "task_cancelled", {"reason": reason})

    # ---------- 单次尝试执行（工作线程，不碰 board） ----------

    def _execute_attempt(self, task, role):
        tid = task["task_id"]
        if role is None:
            return {"status": STATUS_FAILED, "stop_reason": "unknown_role",
                    "error": f"role {task['role']!r} not found", "result": None,
                    "usage": {}, "child_run_id": "", "changed_files": []}

        board = self._board
        dep_results = {d: (board.task(d) or {}).get("result") for d in task["depends_on"]}
        retry_error = task["error"] if task["attempt"] > 1 else ""
        file_summaries = self._fresh_file_summaries(task["inputs"].get("files", []))
        budget_chars = self._child_budget_chars(role)
        brief = build_brief(task, dep_results=dep_results, file_summaries=file_summaries,
                            retry_error=retry_error, max_chars=max(800, int(budget_chars * 0.15)))
        if role.instructions:
            brief = "## Role: " + role.name + "\n" + role.instructions + "\n\n" + brief

        # worker 隔离：git 可用时在独立 worktree 里跑
        worktree_path = None
        branch = ""
        use_worktree = self._git and not role.read_only
        if use_worktree:
            branch = f"pico/{board.run_id}/{tid}"
            with self._git_lock:
                ok, worktree_path, msg = self._wt.add_worktree(tid, board.base_commit, branch)
            if not ok:
                return {"status": STATUS_FAILED, "stop_reason": "worktree_failed",
                        "error": msg, "result": None, "usage": {}, "child_run_id": "", "changed_files": []}

        child = self._build_child(task, role, worktree_path)
        before = {}
        if not role.read_only and not use_worktree:
            before = self.agent.capture_workspace_snapshot()

        started = time.monotonic()
        try:
            final = child.ask(brief)
            child_error = ""
        except Exception as exc:  # noqa: BLE001
            final, child_error = "", f"child agent raised: {exc}"
        duration_ms = int((time.monotonic() - started) * 1000)

        child_run_id = child.current_task_state.run_id if child.current_task_state else ""
        usage = {
            "steps": int(child.current_task_state.tool_steps) if child.current_task_state else 0,
            "prompt_tokens_est": self._estimate_child_tokens(child),
            "duration_ms": duration_ms,
        }
        self.budget.record_usage(steps=usage["steps"], prompt_tokens=usage["prompt_tokens_est"])

        outcome = {"child_run_id": child_run_id, "usage": usage, "branch": branch,
                   "worktree": worktree_path or "", "changed_files": []}

        if child_error:
            outcome.update({"status": STATUS_FAILED, "stop_reason": "model_error", "error": child_error, "result": None})
            return outcome

        result, parse_ok = parse_result(final)

        # 真实改动覆盖模型自述
        changed = []
        if not role.read_only:
            if use_worktree:
                with self._git_lock:
                    self._wt.commit_all(tid, f"pico worker {tid}")
                    changed = self._wt.changed_files(tid, board.base_commit)
            else:
                after = self.agent.capture_workspace_snapshot()
                changed, _ = self.agent.diff_workspace_snapshots(before, after)
            result["changed_files"] = changed
        outcome["changed_files"] = changed

        # 越界检查
        if not role.read_only and task["write_scope"]:
            violations = [p for p in changed if not path_in_scope(p, task["write_scope"])]
            if violations:
                result["status"] = "failed"
                outcome.update({"status": STATUS_SCOPE_VIOLATION, "stop_reason": "scope_violation",
                                "error": "wrote outside write_scope: " + ", ".join(sorted(violations)), "result": result})
                return outcome

        if not parse_ok:
            outcome.update({"status": STATUS_FAILED, "stop_reason": "result_parse_failed",
                            "error": "child <final> was not valid Result JSON", "result": result})
            return outcome

        # 子 agent 自报卡住：记为 blocked，带上它提出的 needs，交回组长用 answer_need 处理。
        if result.get("status") == "blocked":
            need_q = "; ".join(n.get("question", "") for n in result.get("needs", []))
            outcome.update({"status": STATUS_BLOCKED, "stop_reason": "needs_info",
                            "error": need_q or "sub-agent reported blocked", "result": result})
            return outcome

        # 验收命令：worktree 里就在 worktree 跑，否则在主工作区跑
        if task["verify"]:
            verify_cwd = worktree_path if (use_worktree and worktree_path) else self.agent.root
            passed, records = run_all(task["verify"], verify_cwd)
            result["tests"] = records
            if not passed:
                failed = [r["cmd"] for r in records if r["exit_code"] != 0]
                outcome.update({"status": STATUS_FAILED, "stop_reason": "verify_failed",
                                "error": "verify failed: " + "; ".join(failed), "result": result})
                return outcome

        outcome.update({"status": STATUS_DONE, "stop_reason": "final_answer_returned", "result": result})
        return outcome

    def _apply_outcome(self, board, task, outcome):
        tid = task["task_id"]
        if not outcome:
            outcome = {"status": STATUS_FAILED, "stop_reason": "model_error", "error": "no outcome", "result": None, "usage": {}}
        board_task = board.task(tid)
        board_task["child_run_id"] = outcome.get("child_run_id", "")
        board_task["branch"] = outcome.get("branch", board_task.get("branch", ""))
        board_task["worktree"] = outcome.get("worktree", board_task.get("worktree", ""))
        status = outcome.get("status", STATUS_FAILED)

        if outcome.get("status") == STATUS_SCOPE_VIOLATION:
            self._emit("scope_violation", {"task_id": tid, "error": outcome.get("error", "")})
        result = outcome.get("result")
        if result and result.get("proposed_memory"):
            self._emit("memory_proposed", {"task_id": tid, "items": result["proposed_memory"]})

        board.record_attempt_end(tid, status, stop_reason=outcome.get("stop_reason", ""),
                                 error=outcome.get("error", ""), result=result, usage=outcome.get("usage") or {})

        if board_task["status"] == "pending":
            self._emit("task_retried", {"task_id": tid, "attempt": board_task["attempt"]})
        elif board_task["status"] == STATUS_BLOCKED:
            self._emit("task_blocked", {"task_id": tid, "needs": (result or {}).get("needs", [])})
        elif board_task["status"] in (STATUS_FAILED, STATUS_SCOPE_VIOLATION):
            self._emit("task_finished", {"task_id": tid, "status": board_task["status"], "stop_reason": board_task["stop_reason"]})
            blocked = board.block_dependents_of(tid)
            if blocked:
                self._emit("tasks_blocked", {"task_ids": blocked, "cause": tid})
        else:
            self._emit("task_finished", {"task_id": tid, "status": board_task["status"],
                                         "changed_files": (result or {}).get("changed_files", [])})

    def _auto_select_variants(self, board):
        """best-of-N：同一 variant_of 组内，保留通过且改动最小的，其余 cancelled。"""
        groups = {}
        for task in board.tasks:
            if task.get("variant_of"):
                groups.setdefault(task["variant_of"], []).append(task)
        for group_id, members in groups.items():
            if any(m["status"] not in (STATUS_DONE, STATUS_FAILED, STATUS_SCOPE_VIOLATION, STATUS_CANCELLED) for m in members):
                continue  # 还有没跑完的
            winners = [m for m in members if m["status"] == STATUS_DONE]
            if len(winners) <= 1:
                continue  # 0 个成功无从选；1 个成功无需淘汰其他
            winners.sort(key=lambda m: len((m.get("result") or {}).get("changed_files", [])))
            best = winners[0]
            for m in members:
                if m is best:
                    continue
                if m["status"] == STATUS_DONE:
                    m["status"] = STATUS_CANCELLED
                    m["stop_reason"] = "variant_not_selected"
            self._emit("variant_selected", {"group": group_id, "winner": best["task_id"]})

    # ---------- 计划增量工具 ----------

    def amend_tasks(self, tasks_raw):
        """新增任务，或修改仍是 pending/blocked 的任务。等价于 plan_tasks 的"改方案"语义。"""
        return self.plan_tasks(tasks_raw)

    def cancel_tasks(self, ids, reason=""):
        board = self._load_or_none()
        if board is None:
            return "error: no task board"
        ids = [ids] if isinstance(ids, str) else list(ids or [])
        if not ids:
            return "error: cancel_tasks requires task_ids"
        cancelled = []
        for tid in ids:
            if board.cancel(tid, reason=reason):
                cancelled.append(tid)
                self._emit("task_cancelled", {"task_id": tid, "reason": reason})
        self._save()
        if not cancelled:
            return "nothing cancelled (tasks missing or already done)"
        return f"cancelled {', '.join(cancelled)}. board:\n" + _format_compact(board.compact())

    def answer_need(self, task_id, text):
        board = self._load_or_none()
        if board is None:
            return "error: no task board"
        if not str(task_id).strip() or not str(text).strip():
            return "error: answer_need requires task_id and text"
        if not board.answer_need(task_id, text):
            return f"error: unknown task_id {task_id!r}"
        self._save()
        self._emit("need_answered", {"task_id": task_id})
        return f"answered need for {task_id}; it is pending again. call run_tasks to retry."

    # ---------- 工具 3：board ----------

    def board_view(self):
        board = self._load_or_none()
        if board is None:
            return "no task board yet. call plan_tasks first."
        return _format_compact(board.compact())

    # ---------- 工具 4：integrate ----------

    def integrate(self, task_ids):
        board = self._load_or_none()
        if board is None:
            return "error: no task board; call plan_tasks first"
        if not self._git:
            return "integrate skipped: not a git repo. workers already wrote the main workspace directly."
        ids = [task_ids] if isinstance(task_ids, str) else list(task_ids or [])
        if not ids:
            ids = [t["task_id"] for t in board.tasks if t["status"] == STATUS_DONE and t.get("branch")]
        branches = []
        for tid in ids:
            task = board.task(tid)
            if task is None:
                return f"error: unknown task_id {tid!r}"
            if task["status"] != STATUS_DONE:
                return f"error: task {tid!r} is not done (status={task['status']}); only done tasks can be integrated"
            if task.get("branch"):
                branches.append(task["branch"])
        if not branches:
            return "nothing to integrate (no done worker branches)"

        integ = Integrator(self.agent.root, board.base_commit, board.run_id)
        self._emit("integrate_attempted", {"branches": branches})
        outcome = integ.merge(branches)
        if outcome.get("conflict"):
            self._merge_conflicts += 1
            self._emit("merge_conflict", {"branch": outcome["conflict"], "files": outcome.get("conflict_files", [])})
            return (f"merge conflict on {outcome['conflict']} (files: {', '.join(outcome.get('conflict_files', []))}). "
                    "Integration aborted; resolve by re-planning a worker to fix the conflict.")
        self._integrator = integ
        diff = integ.integrated_diff()
        return f"integrated {len(outcome['merged'])} branch(es); integration diff touches {len(diff)} file(s): {', '.join(diff[:20])}"

    # ---------- 工具 5：apply_integration ----------

    def apply_integration(self):
        board = self._load_or_none()
        if board is None:
            return "error: no task board"
        if not self._git:
            return "apply_integration skipped: not a git repo (changes already in main workspace)."
        integ = getattr(self, "_integrator", None)
        if integ is None:
            return "error: nothing integrated yet; call integrate(task_ids) first"
        # reviewer 必须已通过（如果 board 里有 reviewer 任务）
        reviewers = [t for t in board.tasks if t["role"] == "reviewer"]
        if reviewers and not any(t["status"] == STATUS_DONE for t in reviewers):
            return "error: a reviewer task must pass before applying integration"
        # 高风险：走审批
        if not self.agent.approve("apply_integration", {"branch": integ.branch}):
            return "apply_integration denied by approval policy"
        ok, msg = integ.apply_to_main()
        self._emit("integration_applied", {"ok": ok, "message": msg})
        if ok:
            integ.cleanup()
            for t in board.tasks:
                if t.get("worktree"):
                    self._wt.remove_worktree(t["task_id"])
            self._wt.prune()
        return "integration applied to main workspace" if ok else f"error: {msg}"

    # ---------- 工具 6：select_variant（手动择优，可选） ----------

    def select_variant(self, group_id, winner):
        board = self._load_or_none()
        if board is None:
            return "error: no task board"
        members = [t for t in board.tasks if t.get("variant_of") == group_id]
        if not members:
            return f"error: no variants for group {group_id!r}"
        win = board.task(winner)
        if win is None or win.get("variant_of") != group_id:
            return f"error: {winner!r} is not a variant of {group_id!r}"
        if win["status"] != STATUS_DONE:
            return f"error: winner {winner!r} is not done"
        for m in members:
            if m["task_id"] != winner and m["status"] == STATUS_DONE:
                m["status"] = STATUS_CANCELLED
                m["stop_reason"] = "variant_not_selected"
        self._emit("variant_selected", {"group": group_id, "winner": winner})
        self._save()
        return f"selected {winner} for group {group_id}"

    # ---------- 汇总指标 ----------

    def summary(self):
        board = self._load_or_none()
        if board is None:
            return {}
        data = summarize_board(board, concurrency_peak=self._concurrency_peak, merge_conflicts=self._merge_conflicts)
        data["budget"] = self.budget.snapshot()
        data["git_isolation"] = self._git
        return data

    # ---------- 子 agent 构造 ----------

    def _build_child(self, task, role, worktree_path=None):
        from ..runtime import Pico
        from ..workspace import WorkspaceContext

        agent = self.agent
        factory = agent.model_client_factory
        client = factory() if factory else agent.model_client
        profile = self._scaled_profile(role)
        parent_run_id = agent.current_task_state.run_id if agent.current_task_state else ""
        workspace = agent.workspace
        if worktree_path:
            workspace = WorkspaceContext.build(worktree_path)   # worker 的根指向隔离 worktree
        child = Pico(
            model_client=client,
            workspace=workspace,
            session_store=agent.session_store.child_store(),
            run_store=agent.run_store,
            approval_policy="auto" if not role.read_only else "never",
            max_steps=role.max_steps,
            max_new_tokens=agent.max_new_tokens,
            depth=agent.depth + 1,
            max_depth=agent.max_depth,
            read_only=role.read_only,
            secret_env_names=agent.secret_env_names,
            shell_env_allowlist=agent.shell_env_allowlist,
            context_profile=profile,
            model_client_factory=factory,
            durable_memory_writes=False,
            parent_run_id=parent_run_id,
            role=role.name,
            allowed_tools=list(role.tools) if role.tools else None,
            cancel_event=self.cancel_event,
            approval_broker=self.broker,
            collab=CollabHandle(self.blackboard(), task["task_id"]),
        )
        child.session["parent_session_id"] = agent.session["id"]
        child.session_store.save(child.session)
        return child

    def _scaled_profile(self, role):
        profile = self.agent.context_profile
        if profile is None or role.budget_ratio >= 1.0:
            return profile
        try:
            from dataclasses import replace
            return replace(
                profile,
                total_chars=max(2000, int(profile.total_chars * role.budget_ratio)),
                section_budgets={k: max(200, int(v * role.budget_ratio)) for k, v in profile.section_budgets.items()},
                token_budget=int(profile.token_budget * role.budget_ratio) if profile.token_budget else None,
            )
        except Exception:  # noqa: BLE001
            return profile

    def _child_budget_chars(self, role):
        profile = self.agent.context_profile
        base = profile.total_chars if profile is not None else 12400
        return int(base * role.budget_ratio)

    def _estimate_child_tokens(self, child):
        try:
            prompts = getattr(child.model_client, "prompts", None)
            if not prompts:
                return 0
            from ..context_budget import estimate_tokens
            return sum(estimate_tokens(p) for p in prompts)
        except Exception:  # noqa: BLE001
            return 0

    def _fresh_file_summaries(self, files):
        out = {}
        try:
            snapshot = self.agent.memory.to_dict().get("file_summaries", {})
        except Exception:  # noqa: BLE001
            return out
        for path in files:
            canonical = self.agent.memory.canonical_path(path)
            entry = snapshot.get(canonical) or snapshot.get(path)
            if entry and entry.get("summary"):
                out[path] = entry["summary"]
        return out

    def _emit(self, event, payload):
        task_state = getattr(self.agent, "current_task_state", None)
        if task_state is not None:
            with self._emit_lock:
                self.agent.emit_trace(task_state, event, payload)


def _format_compact(compact):
    lines = []
    for row in compact["tasks"]:
        deps = ("<-" + ",".join(row["depends_on"])) if row["depends_on"] else ""
        reason = f" ({row['stop_reason']})" if row["stop_reason"] else ""
        lines.append(f"- {row['task_id']} [{row['role']}] {row['status']}{reason} {deps}  {row['goal']}")
    return "\n".join(lines) if lines else "(no tasks)"
