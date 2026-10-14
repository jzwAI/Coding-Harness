"""多 agent 编排（Phase 1）测试：角色注册表、任务看板、上下文交接、端到端串行执行。"""

import json
import sys

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.agents import RoleRegistry, role_from_text
from pico.orchestration import TaskBoard, BoardStore, new_task, path_in_scope, build_brief, parse_result
from pico.orchestration.task_board import BoardError


# ---------------- 角色注册表 ----------------

def test_builtin_roles_load_with_expected_capabilities():
    roles = RoleRegistry.load()
    assert set(roles.names()) == {"explorer", "worker", "reviewer", "planner"}
    assert roles["explorer"].read_only is True
    assert roles["worker"].read_only is False
    assert "patch_file" in roles["worker"].tools
    assert roles["worker"].max_steps == 12
    assert roles["worker"].write_scope_mode == "declared"
    assert abs(roles["explorer"].budget_ratio - 0.25) < 1e-9


def test_role_rejects_readonly_with_write_tool():
    with pytest.raises(ValueError, match="read-only"):
        role_from_text("---\nname: bad\nwrite_scope: none\ntools: [write_file]\n---\nbody")


def test_role_rejects_bad_budget_and_unknown_tool():
    with pytest.raises(ValueError):
        role_from_text("---\nname: bad\nbudget_ratio: 2\n---\nbody")
    with pytest.raises(ValueError, match="unknown tools"):
        role_from_text("---\nname: bad\ntools: [frobnicate]\n---\nbody")


def test_user_role_overrides_builtin(tmp_path):
    (tmp_path / "explorer.md").write_text(
        "---\nname: explorer\nmax_steps: 99\ntools: [read_file]\nwrite_scope: none\n---\noverride",
        encoding="utf-8",
    )
    roles = RoleRegistry.load(user_dir=tmp_path)
    assert roles["explorer"].max_steps == 99


# ---------------- 任务看板 ----------------

def _mk(**kw):
    base = {"task_id": "t", "role": "worker", "goal": "g"}
    base.update(kw)
    return new_task(base)


ROLES = {"explorer", "worker", "reviewer"}


def test_board_validate_accepts_well_formed_plan():
    board = TaskBoard("run1", tasks=[
        _mk(task_id="t1", role="explorer", write_scope=[]),
        _mk(task_id="t2", depends_on=["t1"], write_scope=["pico/a.py"]),
    ])
    assert board.validate(ROLES) is True
    assert [t["task_id"] for t in board.ready_tasks()] == ["t1"]


def test_board_rejects_duplicate_cycle_unknown_role():
    with pytest.raises(BoardError, match="duplicate"):
        TaskBoard("r", tasks=[_mk(task_id="x"), _mk(task_id="x")]).validate(ROLES)
    with pytest.raises(BoardError, match="unknown role"):
        TaskBoard("r", tasks=[_mk(task_id="x", role="ghost")]).validate(ROLES)
    with pytest.raises(BoardError, match="cycle"):
        TaskBoard("r", tasks=[
            _mk(task_id="a", depends_on=["b"]),
            _mk(task_id="b", depends_on=["a"]),
        ]).validate(ROLES)


def test_board_rejects_parallel_write_scope_overlap():
    with pytest.raises(BoardError, match="write_scope overlaps"):
        TaskBoard("r", tasks=[
            _mk(task_id="a", write_scope=["pico/x.py"]),
            _mk(task_id="b", write_scope=["pico/x.py"]),
        ]).validate(ROLES)


def test_board_allows_overlap_when_dependency_orders_them():
    board = TaskBoard("r", tasks=[
        _mk(task_id="a", write_scope=["pico/x.py"]),
        _mk(task_id="b", depends_on=["a"], write_scope=["pico/x.py"]),
    ])
    assert board.validate(ROLES) is True


def test_board_allows_overlap_for_variants():
    board = TaskBoard("r", tasks=[
        _mk(task_id="a", write_scope=["pico/x.py"], variant_of="orig"),
        _mk(task_id="b", write_scope=["pico/x.py"], variant_of="orig"),
    ])
    assert board.validate(ROLES) is True


def test_board_detects_directory_scope_overlap():
    with pytest.raises(BoardError):
        TaskBoard("r", tasks=[
            _mk(task_id="a", write_scope=["pico/"]),
            _mk(task_id="b", write_scope=["pico/x.py"]),
        ]).validate(ROLES)


def test_path_in_scope_handles_files_and_dirs():
    assert path_in_scope("pico/a.py", ["pico/a.py"])
    assert path_in_scope("pico/sub/x.py", ["pico/"])
    assert not path_in_scope("tests/x.py", ["pico/"])


def test_retry_returns_task_to_pending_until_max_attempts():
    from pico.orchestration.task_board import STATUS_FAILED
    board = TaskBoard("r", tasks=[_mk(task_id="t1", max_attempts=2)])
    board.mark_running("t1", "runA", "m")
    board.record_attempt_end("t1", STATUS_FAILED, stop_reason="verify_failed", error="boom")
    assert board.task("t1")["status"] == "pending"
    assert board.task("t1")["attempt"] == 2
    board.mark_running("t1", "runB", "m")
    board.record_attempt_end("t1", STATUS_FAILED, stop_reason="verify_failed", error="again")
    assert board.task("t1")["status"] == "failed"
    assert len(board.task("t1")["attempts_history"]) == 2


def test_block_dependents_cascades():
    from pico.orchestration.task_board import STATUS_FAILED
    board = TaskBoard("r", tasks=[
        _mk(task_id="a", max_attempts=1),
        _mk(task_id="b", depends_on=["a"]),
        _mk(task_id="c", depends_on=["b"]),
    ])
    board.mark_running("a", "r", "m")
    board.record_attempt_end("a", STATUS_FAILED, stop_reason="x")
    blocked = board.block_dependents_of("a")
    assert set(blocked) == {"b", "c"}
    assert board.task("b")["stop_reason"] == "dependency_failed"


def test_recover_running_resets_without_counting_attempt():
    board = TaskBoard("r", tasks=[_mk(task_id="a")])
    board.mark_running("a", "r", "m")
    assert board.recover_running() == ["a"]
    assert board.task("a")["status"] == "pending"
    assert board.task("a")["attempt"] == 1


def test_board_store_atomic_roundtrip(tmp_path):
    store = BoardStore(tmp_path)
    board = TaskBoard("run9", base_commit="abc", tasks=[_mk(task_id="t1")])
    store.save(board)
    assert store.exists()
    loaded = store.load()
    assert loaded.run_id == "run9"
    assert loaded.base_commit == "abc"
    assert loaded.task("t1")["goal"] == "g"


# ---------------- 上下文交接 ----------------

def test_build_brief_includes_goal_scope_facts_deps_retry():
    task = _mk(task_id="t2", goal="add token budget", acceptance=["tests pass"],
               write_scope=["pico/context_manager.py"],
               inputs={"files": ["pico/context_manager.py"], "facts": ["chars render, token caps"]},
               depends_on=["t1"])
    brief = build_brief(task, dep_results={"t1": {"summary": "explored budget code"}},
                        file_summaries={"pico/context_manager.py": "builds prompt"},
                        retry_error="prev test X failed")
    assert "add token budget" in brief
    assert "scope violation" in brief.lower()
    assert "chars render" in brief
    assert "explored budget code" in brief
    assert "prev test X failed" in brief
    assert "builds prompt" in brief


def test_build_brief_truncates_to_budget():
    assert len(build_brief(_mk(goal="G" * 9000), max_chars=500)) <= 500


def test_parse_result_valid_and_embedded_and_garbage():
    res, ok = parse_result('<final>{"status":"done","summary":"ok","changed_files":["a.py"],'
                           '"tests":[{"cmd":"pytest","exit_code":0}],"confidence":0.9}</final>')
    assert ok and res["status"] == "done" and res["changed_files"] == ["a.py"]
    assert res["tests"][0]["exit_code"] == 0 and res["parse_ok"]

    res, ok = parse_result('Sure!\n{"status":"blocked","summary":"need info","open_questions":["which api?"]}\nThanks')
    assert ok and res["status"] == "blocked" and res["open_questions"] == ["which api?"]

    res, ok = parse_result("I could not finish, sorry.")
    assert not ok and res["status"] == "failed" and res["parse_ok"] is False
    assert "could not finish" in res["summary"]


# ---------------- 端到端 ----------------

def _parent(tmp_path, outputs, child_factory):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        max_steps=8,
        model_client_factory=child_factory,
    )


def _tool(name, args):
    return "<tool>" + json.dumps({"name": name, "args": args}) + "</tool>"


def test_orchestration_tools_only_at_depth_zero(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    parent = Pico(model_client=FakeModelClient([]), workspace=workspace, session_store=store, approval_policy="auto")
    child = Pico(model_client=FakeModelClient([]), workspace=workspace, session_store=store,
                 approval_policy="auto", depth=1)
    assert "plan_tasks" in parent.tools and "run_tasks" in parent.tools and "board" in parent.tools
    assert "plan_tasks" not in child.tools


def test_end_to_end_plan_run_explorer_then_worker(tmp_path):
    child_scripts = [
        ['<final>{"status":"done","summary":"explored the layout"}</final>'],
        ['<tool name="write_file" path="out.txt"><content>hi\n</content></tool>',
         '<final>{"status":"done","summary":"wrote out.txt","changed_files":["out.txt"]}</final>'],
    ]
    state = {"i": 0}

    def factory():
        script = child_scripts[state["i"]]
        state["i"] += 1
        return FakeModelClient(list(script))

    tasks = [
        {"task_id": "t1", "role": "explorer", "goal": "inspect repo"},
        {"task_id": "t2", "role": "worker", "goal": "create out.txt",
         "depends_on": ["t1"], "write_scope": ["out.txt"]},
    ]
    outputs = [
        _tool("plan_tasks", {"tasks": tasks}),
        _tool("run_tasks", {"task_ids": "ready"}),   # t1
        _tool("run_tasks", {"task_ids": ["t2"]}),    # t2
        "<final>All tasks complete.</final>",
    ]
    agent = _parent(tmp_path, outputs, factory)
    answer = agent.ask("Build out.txt via sub-agents")

    assert answer == "All tasks complete."
    # worker 实际写了文件
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hi\n"
    # board 落盘且两任务 done
    board = BoardStore(agent.current_run_dir).load()
    assert board.task("t1")["status"] == "done"
    assert board.task("t2")["status"] == "done"
    # changed_files 用真实 diff 覆盖
    assert board.task("t2")["result"]["changed_files"] == ["out.txt"]
    assert board.task("t2")["child_run_id"]
    # trace 事件
    trace = agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8")
    assert "tasks_planned" in trace and "task_started" in trace and "task_finished" in trace


def test_end_to_end_scope_violation_blocks_merge(tmp_path):
    child_scripts = [
        ['<tool name="write_file" path="out.txt"><content>oops\n</content></tool>',
         '<final>{"status":"done","summary":"wrote the wrong file","changed_files":["out.txt"]}</final>'],
    ]
    state = {"i": 0}

    def factory():
        script = child_scripts[min(state["i"], len(child_scripts) - 1)]
        state["i"] += 1
        return FakeModelClient(list(script))

    tasks = [{
        "task_id": "t1", "role": "worker", "goal": "write allowed.txt only",
        "write_scope": ["allowed.txt"], "max_attempts": 1,
    }]
    outputs = [
        _tool("plan_tasks", {"tasks": tasks}),
        _tool("run_tasks", {"task_ids": "ready"}),
        "<final>done</final>",
    ]
    agent = _parent(tmp_path, outputs, factory)
    agent.ask("try to write out of scope")

    board = BoardStore(agent.current_run_dir).load()
    assert board.task("t1")["status"] == "scope_violation"
    assert "out.txt" in board.task("t1")["error"]
    trace = agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8")
    assert "scope_violation" in trace


def test_end_to_end_verify_failure_marks_failed(tmp_path):
    child_scripts = [
        ['<final>{"status":"done","summary":"did nothing useful"}</final>'],
    ]
    state = {"i": 0}

    def factory():
        state["i"] += 1
        return FakeModelClient(list(child_scripts[0]))

    tasks = [{
        "task_id": "t1", "role": "reviewer", "goal": "verify something",
        "verify": [f'{sys.executable} -c "import sys; sys.exit(1)"'], "max_attempts": 1,
    }]
    outputs = [
        _tool("plan_tasks", {"tasks": tasks}),
        _tool("run_tasks", {"task_ids": "ready"}),
        "<final>done</final>",
    ]
    agent = _parent(tmp_path, outputs, factory)
    agent.ask("run a failing verify")

    board = BoardStore(agent.current_run_dir).load()
    assert board.task("t1")["status"] == "failed"
    assert board.task("t1")["stop_reason"] == "verify_failed"


def test_plan_tasks_tool_reports_validation_error(tmp_path):
    tasks = [
        {"task_id": "a", "role": "worker", "goal": "x", "write_scope": ["shared.py"]},
        {"task_id": "b", "role": "worker", "goal": "y", "write_scope": ["shared.py"]},
    ]
    outputs = [
        _tool("plan_tasks", {"tasks": tasks}),
        "<final>stopped</final>",
    ]
    agent = _parent(tmp_path, outputs, lambda: FakeModelClient([]))
    agent.ask("plan overlapping tasks")
    tool_events = [i for i in agent.session["history"] if i["role"] == "tool" and i["name"] == "plan_tasks"]
    assert tool_events
    assert "error" in tool_events[0]["content"] and "write_scope overlaps" in tool_events[0]["content"]
