"""Phase 2/3/4 测试：并行调度、预算、审批、取消、worktree 隔离、合并、best-of-N、CLI。

需要 git 的测试会在 tmp_path 里 `git init` 自建仓库；没有 git 时自动跳过。
"""

import json
import shutil
import subprocess
import threading
import time

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.orchestration import BoardStore
from pico.orchestration.budget import BudgetLedger
from pico.orchestration.approval import ApprovalBroker
from pico.orchestration.metrics import summarize_board
from pico.orchestration.task_board import TaskBoard, new_task, STATUS_DONE, STATUS_FAILED


GIT = shutil.which("git") is not None


def _tool(name, args):
    return "<tool>" + json.dumps({"name": name, "args": args}) + "</tool>"


def _mk(**kw):
    d = {"task_id": "t", "role": "worker", "goal": "g"}
    d.update(kw)
    return new_task(d)


def _parent(tmp_path, outputs, factory, **cfg):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        max_steps=10,
        model_client_factory=factory,
        orchestration_config=cfg or None,
    )


def _git_init(tmp_path):
    def g(*args):
        return subprocess.run(["git", *args], cwd=str(tmp_path), capture_output=True, text=True, check=False)
    g("init")
    g("config", "user.email", "test@local")
    g("config", "user.name", "test")
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    g("add", "-A")
    g("-c", "user.email=test@local", "-c", "user.name=test", "commit", "-m", "init")


# ---------------- 预算账本 ----------------

def test_budget_ledger_limits_and_thread_safe():
    b = BudgetLedger(max_total_steps=10_000, max_total_prompt_tokens=10_000_000, max_tasks=10_000)

    def worker():
        for _ in range(100):
            b.try_start_task()
            b.record_usage(steps=1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert b.started_tasks == 800 and b.used_steps == 800

    capped = BudgetLedger(max_tasks=3)
    assert capped.try_start_task()[0]
    assert capped.try_start_task()[0]
    assert capped.try_start_task()[0]
    ok, reason = capped.try_start_task()
    assert ok is False and "max_tasks" in reason


def test_budget_ledger_step_cap_blocks_new_tasks():
    b = BudgetLedger(max_total_steps=5, max_tasks=1000)
    b.try_start_task()
    b.record_usage(steps=5)
    assert b.exhausted()
    assert b.try_start_task()[0] is False


# ---------------- 审批 broker ----------------

def test_approval_broker_policies_and_source_label():
    assert ApprovalBroker("auto").request("t2/worker", "run_shell", {}) is True
    assert ApprovalBroker("never").request("t2/worker", "run_shell", {}) is False
    seen = {}
    broker = ApprovalBroker("ask", prompt_func=lambda m: seen.setdefault("msg", m) and "y" or "y")
    assert broker.request("t2/worker", "run_shell", {"cmd": "x"}) is True
    assert "t2/worker" in seen["msg"]
    assert ApprovalBroker("ask", prompt_func=lambda m: "n").request("t", "run_shell", {}) is False


# ---------------- 指标汇总 ----------------

def test_summarize_board_computes_speedup_and_counts():
    board = TaskBoard("r", tasks=[_mk(task_id="a"), _mk(task_id="b"), _mk(task_id="c", max_attempts=2)])
    board.mark_running("a", "r", "m")
    board.record_attempt_end("a", STATUS_DONE, result={"changed_files": ["x"]},
                             usage={"steps": 2, "prompt_tokens_est": 50, "duration_ms": 100})
    board.mark_running("b", "r", "m")
    board.record_attempt_end("b", STATUS_DONE, result={"changed_files": ["y", "z"]},
                             usage={"steps": 3, "prompt_tokens_est": 70, "duration_ms": 200})
    board.mark_running("c", "r", "m")
    board.record_attempt_end("c", STATUS_FAILED, stop_reason="verify_failed", usage={"steps": 1, "duration_ms": 50})
    board.mark_running("c", "r", "m")
    board.record_attempt_end("c", STATUS_FAILED, stop_reason="verify_failed", usage={"steps": 1, "duration_ms": 50})
    s = summarize_board(board, wall_clock_ms=250, concurrency_peak=2)
    assert s["task_count"] == 3
    assert s["sum_task_duration_ms"] == 400
    assert abs(s["speedup"] - (400 / 250)) < 1e-6
    assert s["retries"] == 1
    assert s["status_counts"]["done"] == 2 and s["status_counts"]["failed"] == 1


# ---------------- 并行调度（无 git：读并行，写串行回退） ----------------

def test_parallel_explorers_all_run(tmp_path):
    scripts = [['<final>{"status":"done","summary":"e%d"}</final>' % i] for i in range(3)]
    state = {"i": 0}

    def factory():
        s = scripts[min(state["i"], len(scripts) - 1)]
        state["i"] += 1
        return FakeModelClient(list(s))

    tasks = [{"task_id": f"e{i}", "role": "explorer", "goal": f"look {i}"} for i in range(3)]
    outputs = [_tool("plan_tasks", {"tasks": tasks}), _tool("run_tasks", {"task_ids": "ready"}), "<final>done</final>"]
    agent = _parent(tmp_path, outputs, factory, max_concurrency=3)
    agent.ask("explore in parallel")
    board = BoardStore(agent.current_run_dir).load()
    assert all(board.task(f"e{i}")["status"] == "done" for i in range(3))
    summary = agent._orchestrator.summary()
    assert summary["concurrency_peak"] >= 2  # 至少两个并发


def test_budget_exhaustion_cancels_remaining(tmp_path):
    scripts = [['<final>{"status":"done","summary":"ok"}</final>'] for _ in range(5)]
    state = {"i": 0}

    def factory():
        s = scripts[min(state["i"], len(scripts) - 1)]
        state["i"] += 1
        return FakeModelClient(list(s))

    tasks = [{"task_id": f"e{i}", "role": "explorer", "goal": "x"} for i in range(5)]
    outputs = [_tool("plan_tasks", {"tasks": tasks}), _tool("run_tasks", {"task_ids": "ready"}), "<final>stopped</final>"]
    agent = _parent(tmp_path, outputs, factory, max_concurrency=1, max_tasks=2)
    agent.ask("run with tiny task budget")
    board = BoardStore(agent.current_run_dir).load()
    done = sum(1 for i in range(5) if board.task(f"e{i}")["status"] == "done")
    assert done <= 2  # 预算只允许 2 个任务启动
    trace = agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8")
    assert "budget_exhausted" in trace


# ---------------- best-of-N ----------------

def test_best_of_n_expands_and_auto_selects(tmp_path):
    # 两个变体都成功，diff 不同大小；自动保留 diff 最小的
    scripts = [
        ['<tool name="write_file" path="sol.txt"><content>one line\n</content></tool>',
         '<final>{"status":"done","summary":"v1","changed_files":["sol.txt"]}</final>'],
        ['<tool name="write_file" path="sol.txt"><content>a\n</content></tool>',
         '<tool name="write_file" path="extra.txt"><content>b\n</content></tool>',
         '<final>{"status":"done","summary":"v2","changed_files":["sol.txt","extra.txt"]}</final>'],
    ]
    state = {"i": 0}

    def factory():
        s = scripts[min(state["i"], len(scripts) - 1)]
        state["i"] += 1
        return FakeModelClient(list(s))

    tasks = [{"task_id": "sol", "role": "worker", "goal": "solve", "variants": 2,
              "write_scope": ["sol.txt", "extra.txt"]}]
    outputs = [_tool("plan_tasks", {"tasks": tasks}), _tool("run_tasks", {"task_ids": "ready"}), "<final>done</final>"]
    agent = _parent(tmp_path, outputs, factory, max_concurrency=1)  # no git: serial writers
    out = agent.ask("best of 2")
    board = BoardStore(agent.current_run_dir).load()
    statuses = {board.task("sol-v1")["status"], board.task("sol-v2")["status"]}
    assert "done" in statuses and "cancelled" in statuses
    winner = [t for t in board.tasks if t["status"] == "done" and t.get("variant_of") == "sol"][0]
    assert winner["task_id"] == "sol-v1"  # diff 最小


# ---------------- git worktree 隔离 + 合并 ----------------

@pytest.mark.skipif(not GIT, reason="git not available")
def test_worktree_isolation_integrate_and_apply(tmp_path):
    _git_init(tmp_path)
    scripts = [
        ['<tool name="write_file" path="a.txt"><content>alpha\n</content></tool>',
         '<final>{"status":"done","summary":"wrote a"}</final>'],
        ['<tool name="write_file" path="b.txt"><content>beta\n</content></tool>',
         '<final>{"status":"done","summary":"wrote b"}</final>'],
    ]
    state = {"i": 0}

    def factory():
        s = scripts[min(state["i"], len(scripts) - 1)]
        state["i"] += 1
        return FakeModelClient(list(s))

    tasks = [
        {"task_id": "ta", "role": "worker", "goal": "write a", "write_scope": ["a.txt"]},
        {"task_id": "tb", "role": "worker", "goal": "write b", "write_scope": ["b.txt"]},
    ]
    outputs = [
        _tool("plan_tasks", {"tasks": tasks}),
        _tool("run_tasks", {"task_ids": "ready"}),      # 两个 worker 并行，各自 worktree
        _tool("integrate", {"task_ids": ["ta", "tb"]}),
        _tool("apply_integration", {}),
        "<final>done</final>",
    ]
    agent = _parent(tmp_path, outputs, factory, max_concurrency=2)
    agent.ask("parallel writers via worktrees")

    board = BoardStore(agent.current_run_dir).load()
    assert board.task("ta")["status"] == "done" and board.task("tb")["status"] == "done"
    # 工作区在跑任务期间不被污染，apply 之后两文件都落到主工作区
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "alpha\n"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "beta\n"
    trace = agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8")
    assert "integrate_attempted" in trace and "integration_applied" in trace


@pytest.mark.skipif(not GIT, reason="git not available")
def test_worktree_worker_does_not_touch_main_until_apply(tmp_path):
    _git_init(tmp_path)
    scripts = [
        ['<tool name="write_file" path="c.txt"><content>gamma\n</content></tool>',
         '<final>{"status":"done","summary":"wrote c"}</final>'],
    ]
    state = {"i": 0}

    def factory():
        state["i"] += 1
        return FakeModelClient(list(scripts[0]))

    tasks = [{"task_id": "tc", "role": "worker", "goal": "write c", "write_scope": ["c.txt"]}]
    outputs = [
        _tool("plan_tasks", {"tasks": tasks}),
        _tool("run_tasks", {"task_ids": "ready"}),
        "<final>paused before integrate</final>",
    ]
    agent = _parent(tmp_path, outputs, factory, max_concurrency=1)
    agent.ask("write but do not integrate")
    # 没有 integrate/apply：主工作区不该出现 c.txt
    assert not (tmp_path / "c.txt").exists()
    board = BoardStore(agent.current_run_dir).load()
    assert board.task("tc")["status"] == "done"


# ---------------- CLI run tree ----------------

def test_render_run_tree(tmp_path):
    from pico.cli import render_run_tree
    runs = tmp_path / ".pico" / "runs"
    (runs / "run_parent").mkdir(parents=True)
    (runs / "run_child").mkdir(parents=True)
    (runs / "run_parent" / "report.json").write_text(
        json.dumps({"run_id": "run_parent", "parent_run_id": "", "role": "", "status": "completed"}), encoding="utf-8")
    (runs / "run_child" / "report.json").write_text(
        json.dumps({"run_id": "run_child", "parent_run_id": "run_parent", "role": "worker", "status": "completed"}), encoding="utf-8")
    tree = render_run_tree(runs, "run_parent")
    assert "run_parent" in tree and "run_child" in tree
    assert tree.index("run_parent") < tree.index("run_child")
