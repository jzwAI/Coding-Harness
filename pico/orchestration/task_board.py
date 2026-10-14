"""TaskBoard：一次多 agent 协作的任务看板。

记录每个任务要做什么、能动哪些文件、依赖谁、现在到哪一步、产出了什么、花了多少。
存成 .pico/runs/<run_id>/board.json，用原子写，崩溃后可恢复。

字段分两类：
- 规划字段：Orchestrator 调用 plan_tasks 时填写，runtime 会校验。
- 系统字段：只能由 runtime 写入，模型填了也会被忽略。
详见 docs/architecture/multi-agent-v1-design.md 第 5 节。
"""

from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BOARD_SCHEMA_VERSION = 1

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"
STATUS_SCOPE_VIOLATION = "scope_violation"
STATUS_CANCELLED = "cancelled"

TERMINAL_STATUSES = {STATUS_DONE, STATUS_CANCELLED}
TASK_ID_PATTERN = re.compile(r"^[a-z0-9_-]+$")
VALID_ROLES_FALLBACK = {"explorer", "worker", "reviewer", "planner"}


def _now():
    return datetime.now(timezone.utc).isoformat()


class BoardError(ValueError):
    """plan_tasks 校验失败时抛出，错误信息直接回给模型。"""


def _as_str_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(item).strip() for item in value if str(item).strip()]


def new_task(raw):
    """从模型给的原始 dict 构造一张规范化的任务卡（只保留规划字段，系统字段置初始值）。"""
    task_id = str(raw.get("task_id", "")).strip()
    return {
        # ---- 规划字段 ----
        "task_id": task_id,
        "role": str(raw.get("role", "")).strip(),
        "goal": str(raw.get("goal", "")).strip(),
        "acceptance": _as_str_list(raw.get("acceptance")),
        "verify": _as_str_list(raw.get("verify")),
        "inputs": {
            "files": _as_str_list((raw.get("inputs") or {}).get("files")),
            "facts": _as_str_list((raw.get("inputs") or {}).get("facts")),
        },
        "depends_on": _as_str_list(raw.get("depends_on")),
        "write_scope": _as_str_list(raw.get("write_scope")),
        "model": str(raw.get("model", "inherit")).strip() or "inherit",
        "variant_of": str(raw.get("variant_of", "")).strip(),
        "max_attempts": _coerce_int(raw.get("max_attempts"), default=2, lo=1, hi=5),
        # ---- 系统字段 ----
        "status": STATUS_PENDING,
        "stop_reason": "",
        "error": "",
        "attempt": 1,
        "child_run_id": "",
        "attempts_history": [],
        "model_resolved": "",
        "worktree": "",
        "branch": "",
        "created_at": _now(),
        "started_at": "",
        "finished_at": "",
        "result": None,
        "usage": {"steps": 0, "prompt_tokens_est": 0, "duration_ms": 0},
    }


def _coerce_int(value, default, lo, hi):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


class TaskBoard:
    def __init__(self, run_id, base_commit="", tasks=None, schema_version=BOARD_SCHEMA_VERSION, created_at=None):
        self.run_id = str(run_id)
        self.base_commit = str(base_commit or "")
        self.schema_version = int(schema_version)
        self.created_at = created_at or _now()
        self.tasks = list(tasks or [])

    # ---------- 序列化 ----------

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "base_commit": self.base_commit,
            "created_at": self.created_at,
            "tasks": self.tasks,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            run_id=data.get("run_id", ""),
            base_commit=data.get("base_commit", ""),
            tasks=list(data.get("tasks", [])),
            schema_version=data.get("schema_version", BOARD_SCHEMA_VERSION),
            created_at=data.get("created_at"),
        )

    # ---------- 查询 ----------

    def task(self, task_id):
        for task in self.tasks:
            if task["task_id"] == task_id:
                return task
        return None

    def ids(self):
        return [task["task_id"] for task in self.tasks]

    def ready_tasks(self):
        """返回所有就绪任务：状态 pending，且依赖全部 done。"""
        done = {t["task_id"] for t in self.tasks if t["status"] == STATUS_DONE}
        ready = []
        for task in self.tasks:
            if task["status"] != STATUS_PENDING:
                continue
            if all(dep in done for dep in task["depends_on"]):
                ready.append(task)
        return ready

    def all_settled(self):
        return all(t["status"] in TERMINAL_STATUSES or t["status"] == STATUS_FAILED or t["status"] == STATUS_BLOCKED or t["status"] == STATUS_SCOPE_VIOLATION for t in self.tasks)

    def compact(self):
        """给模型看的精简视图：只保留关键字段，避免把整块 result 塞进 prompt。"""
        rows = []
        for t in self.tasks:
            rows.append({
                "task_id": t["task_id"],
                "role": t["role"],
                "status": t["status"],
                "goal": t["goal"][:120],
                "depends_on": t["depends_on"],
                "attempt": t["attempt"],
                "stop_reason": t["stop_reason"],
            })
        return {"run_id": self.run_id, "tasks": rows}

    # ---------- 校验 ----------

    def validate(self, known_roles=None):
        """plan_tasks 时的全量校验，失败抛 BoardError。"""
        known_roles = set(known_roles or VALID_ROLES_FALLBACK)
        ids = self.ids()

        # 1. task_id 唯一且格式合法
        seen = set()
        for task in self.tasks:
            tid = task["task_id"]
            if not tid or not TASK_ID_PATTERN.match(tid):
                raise BoardError(f"invalid task_id {tid!r}: use only [a-z0-9_-]")
            if tid in seen:
                raise BoardError(f"duplicate task_id {tid!r}")
            seen.add(tid)

        for task in self.tasks:
            tid = task["task_id"]
            # 2. 角色存在
            if task["role"] not in known_roles:
                raise BoardError(f"task {tid!r} uses unknown role {task['role']!r}; known: {', '.join(sorted(known_roles))}")
            # 3. goal 非空
            if not task["goal"]:
                raise BoardError(f"task {tid!r} has empty goal")
            # 4. 依赖存在
            for dep in task["depends_on"]:
                if dep not in ids:
                    raise BoardError(f"task {tid!r} depends on unknown task {dep!r}")
                if dep == tid:
                    raise BoardError(f"task {tid!r} depends on itself")

        # 5. 无环
        self._assert_acyclic()

        # 6. write_scope：可能并行的 worker 之间不能重叠
        self._assert_write_scope_disjoint()

        return True

    def _assert_acyclic(self):
        # Kahn 拓扑排序：能排完说明无环。
        indeg = {t["task_id"]: 0 for t in self.tasks}
        adj = {t["task_id"]: [] for t in self.tasks}
        for task in self.tasks:
            for dep in task["depends_on"]:
                adj[dep].append(task["task_id"])
                indeg[task["task_id"]] += 1
        queue = [tid for tid, d in indeg.items() if d == 0]
        visited = 0
        while queue:
            node = queue.pop()
            visited += 1
            for nxt in adj[node]:
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    queue.append(nxt)
        if visited != len(self.tasks):
            raise BoardError("dependency cycle detected among tasks")

    def _reachable(self, start):
        """start 的所有祖先 + 后代（即有依赖先后关系的任务集合，含自身）。"""
        up = {t["task_id"]: set(t["depends_on"]) for t in self.tasks}
        down = {t["task_id"]: set() for t in self.tasks}
        for tid, deps in up.items():
            for d in deps:
                if d in down:
                    down[d].add(tid)

        def walk(seed, graph):
            seen = set()
            stack = [seed]
            while stack:
                node = stack.pop()
                for nxt in graph.get(node, ()):
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
            return seen

        return walk(start, up) | walk(start, down) | {start}

    def _assert_write_scope_disjoint(self):
        """任意两个"可能同时运行"的 worker，write_scope 不能有交集。

        "可能同时运行" = 彼此之间没有依赖先后关系，且不是同一组 variant_of 候选。
        """
        workers = [t for t in self.tasks if t["write_scope"]]
        for i in range(len(workers)):
            for j in range(i + 1, len(workers)):
                a, b = workers[i], workers[j]
                # 同一组 best-of-N 候选：它们本就是竞争关系，允许重叠
                if a["variant_of"] and a["variant_of"] == b["variant_of"]:
                    continue
                if b["task_id"] in self._reachable(a["task_id"]):
                    continue  # 有依赖先后关系，不会同时跑
                overlap = _scope_overlap(a["write_scope"], b["write_scope"])
                if overlap:
                    raise BoardError(
                        f"tasks {a['task_id']!r} and {b['task_id']!r} may run in parallel but their "
                        f"write_scope overlaps on: {', '.join(sorted(overlap))}"
                    )

    # ---------- 状态流转 ----------

    def mark_running(self, task_id, child_run_id, model_resolved):
        task = self.task(task_id)
        task["status"] = STATUS_RUNNING
        task["child_run_id"] = child_run_id
        task["model_resolved"] = model_resolved
        task["started_at"] = _now()
        return task

    def record_attempt_end(self, task_id, status, stop_reason="", error="", result=None, usage=None):
        """一次尝试结束：写入状态/结果，追加 attempts_history，必要时安排重试。"""
        task = self.task(task_id)
        task["finished_at"] = _now()
        task["stop_reason"] = stop_reason
        task["error"] = str(error or "")[:500]
        if result is not None:
            task["result"] = result
        if usage:
            task["usage"] = usage
        task["attempts_history"].append({
            "attempt": task["attempt"],
            "child_run_id": task["child_run_id"],
            "model_resolved": task["model_resolved"],
            "status": status,
            "stop_reason": stop_reason,
            "error": task["error"],
            "started_at": task["started_at"],
            "finished_at": task["finished_at"],
            "usage": task["usage"],
        })

        if status in (STATUS_FAILED, STATUS_SCOPE_VIOLATION) and task["attempt"] < task["max_attempts"]:
            # 还能重试：回到 pending，attempt+1；error 会被编入下一次 Brief。
            task["status"] = STATUS_PENDING
            task["attempt"] += 1
        else:
            task["status"] = status
        return task

    def block_dependents_of(self, task_id):
        """某任务进入非 done 终态后，把依赖它的任务标记为 blocked。"""
        bad = {t["task_id"] for t in self.tasks if t["status"] in (STATUS_FAILED, STATUS_SCOPE_VIOLATION, STATUS_CANCELLED, STATUS_BLOCKED)}
        changed = []
        progressing = True
        while progressing:
            progressing = False
            for task in self.tasks:
                if task["status"] != STATUS_PENDING:
                    continue
                if any(dep in bad for dep in task["depends_on"]):
                    task["status"] = STATUS_BLOCKED
                    task["stop_reason"] = "dependency_failed"
                    bad.add(task["task_id"])
                    changed.append(task["task_id"])
                    progressing = True
        return changed

    def cancel(self, task_id, reason=""):
        """取消 pending/blocked 任务，并级联 block 依赖它的任务。done 任务不可取消。"""
        task = self.task(task_id)
        if task is None or task["status"] == STATUS_DONE:
            return False
        task["status"] = STATUS_CANCELLED
        task["stop_reason"] = reason or "cancelled_by_orchestrator"
        self.block_dependents_of(task_id)
        return True

    def answer_need(self, task_id, text):
        """回答 blocked 任务的 need：把答案并入 facts，blocked -> pending 以便重试。"""
        task = self.task(task_id)
        if task is None:
            return False
        facts = task.setdefault("inputs", {}).setdefault("facts", [])
        facts.append("Answer to earlier open need: " + str(text).strip())
        if task["status"] == STATUS_BLOCKED:
            task["status"] = STATUS_PENDING
            task["stop_reason"] = ""
            task["error"] = ""
        return True

    def recover_running(self):
        """崩溃恢复：running 改回 pending（不算失败，attempt 不加）。"""
        recovered = []
        for task in self.tasks:
            if task["status"] == STATUS_RUNNING:
                task["status"] = STATUS_PENDING
                task["started_at"] = ""
                task["child_run_id"] = ""
                recovered.append(task["task_id"])
        return recovered


def _normalize_scope_entry(entry):
    entry = str(entry).strip().replace("\\", "/").lstrip("./")
    return entry.rstrip("/") + "/" if entry.endswith("/") else entry


def _scope_overlap(scope_a, scope_b):
    """返回两个 write_scope 的冲突项。目录（以 / 结尾）覆盖其下所有文件。"""
    a = [_normalize_scope_entry(x) for x in scope_a]
    b = [_normalize_scope_entry(x) for x in scope_b]
    overlap = set()
    for x in a:
        for y in b:
            if _paths_conflict(x, y):
                overlap.add(x)
                overlap.add(y)
    return overlap


def _paths_conflict(x, y):
    x_dir, y_dir = x.endswith("/"), y.endswith("/")
    if x_dir and y_dir:
        return x.startswith(y) or y.startswith(x)
    if x_dir:
        return y.startswith(x)
    if y_dir:
        return x.startswith(y)
    return x == y


def path_in_scope(path, write_scope):
    """判断某个实际改动的文件是否落在声明的 write_scope 内。"""
    path = _normalize_scope_entry(path)
    for entry in write_scope:
        entry = _normalize_scope_entry(entry)
        if entry.endswith("/"):
            if path.startswith(entry):
                return True
        elif path == entry:
            return True
    return False


class BoardStore:
    """board.json 的原子读写。"""

    def __init__(self, run_dir):
        self.path = Path(run_dir) / "board.json"

    def exists(self):
        return self.path.exists()

    def save(self, board):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False,
            dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp",
        ) as handle:
            json.dump(board.to_dict(), handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            temp_name = handle.name
        Path(temp_name).replace(self.path)
        return self.path

    def load(self):
        return TaskBoard.from_dict(json.loads(self.path.read_text(encoding="utf-8")))
