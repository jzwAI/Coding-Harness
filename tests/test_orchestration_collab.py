"""v2 协作测试：黑板、信箱、needs/answer_need、wave 调度 + 进度摘要、计划增量。"""

import json

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.orchestration import BoardStore, Blackboard, CollabHandle
from pico.orchestration.handoff import parse_result
from pico.orchestration.task_board import (
    TaskBoard, new_task, STATUS_BLOCKED, STATUS_PENDING, STATUS_DONE, STATUS_CANCELLED,
)


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
        max_steps=12,
        model_client_factory=factory,
        orchestration_config=cfg or None,
    )


# ---------------- 黑板 ----------------

def test_blackboard_post_read_prefix_and_identity(tmp_path):
    bb = Blackboard(store_path=tmp_path / "blackboard.json")
    h2, h3 = CollabHandle(bb, "t2"), CollabHandle(bb, "t3")
    h2.post_note("api.sig", "f(x)->int", evidence="m.py:1", kind="interface")
    h2.post_note("other.k", "v")
    read = h3.read_notes(key_prefix="api.")
    assert "f(x)->int" in read and "m.py:1" in read
    assert "other.k" not in read
    assert all(n["from_task"] == "t2" for n in bb.read())
    assert (tmp_path / "blackboard.json").exists()


def test_mailbox_send_and_consume_once(tmp_path):
    bb = Blackboard(store_path=tmp_path / "blackboard.json")
    h2, h3 = CollabHandle(bb, "t2"), CollabHandle(bb, "t3")
    h2.send_message("t3", "interface frozen")
    assert "interface frozen" in h3.check_inbox()
    assert h3.check_inbox() == "(inbox empty)"   # 已读后清空


def test_blackboard_concurrent_posts_are_safe():
    import threading
    bb = Blackboard()

    def poster(tid):
        h = CollabHandle(bb, tid)
        for i in range(50):
            h.post_note(f"k{tid}.{i}", "x")

    threads = [threading.Thread(target=poster, args=(f"t{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    notes = bb.read(limit=10000)
    assert len(notes) == 400
    assert len({n["id"] for n in notes}) == 400


# ---------------- needs / cancel / answer ----------------

def test_parse_result_extracts_needs():
    res, ok = parse_result('{"status":"blocked","summary":"stuck",'
                           '"needs":[{"question":"which key?","suggested_source":"config.py"}]}')
    assert ok and res["status"] == "blocked"
    assert res["needs"][0]["question"] == "which key?"


def test_board_cancel_cascades_and_done_is_protected():
    board = TaskBoard("r", tasks=[_mk(task_id="a"), _mk(task_id="b", depends_on=["a"]),
                                  _mk(task_id="c", depends_on=["b"])])
    assert board.cancel("a", "wrong approach")
    assert board.task("a")["status"] == STATUS_CANCELLED
    assert board.task("b")["status"] == "blocked" and board.task("c")["status"] == "blocked"

    done = TaskBoard("r", tasks=[_mk(task_id="d")])
    done.mark_running("d", "r", "m")
    done.record_attempt_end("d", STATUS_DONE, result={"summary": "ok"})
    assert done.cancel("d") is False and done.task("d")["status"] == STATUS_DONE


def test_board_answer_need_unblocks_and_injects_fact():
    board = TaskBoard("r", tasks=[_mk(task_id="x")])
    board.task("x")["status"] = STATUS_BLOCKED
    assert board.answer_need("x", "use /v1/internal")
    assert board.task("x")["status"] == STATUS_PENDING
    assert any("/v1/internal" in f for f in board.task("x")["inputs"]["facts"])


# ---------------- 端到端：协作工具可用 ----------------

def test_worker_can_post_and_read_blackboard(tmp_path):
    # t1 把结论贴上黑板；t2 读到后据此完成（都是脚本化的假模型）
    scripts = [
        [_tool("post_note", {"key": "api.sig", "text": "add(a,b)->int", "evidence": "m.py:1"}),
         '<final>{"status":"done","summary":"posted api.sig"}</final>'],
        [_tool("read_notes", {"key_prefix": "api."}),
         '<final>{"status":"done","summary":"read the interface"}</final>'],
    ]
    state = {"i": 0}

    def factory():
        s = scripts[min(state["i"], len(scripts) - 1)]
        state["i"] += 1
        return FakeModelClient(list(s))

    tasks = [
        {"task_id": "t1", "role": "explorer", "goal": "post interface"},
        {"task_id": "t2", "role": "explorer", "goal": "use interface", "depends_on": ["t1"]},
    ]
    outputs = [
        _tool("plan_tasks", {"tasks": tasks}),
        _tool("run_tasks", {"task_ids": "ready"}),   # t1
        _tool("run_tasks", {"task_ids": "ready"}),   # t2
        "<final>done</final>",
    ]
    agent = _parent(tmp_path, outputs, factory, max_concurrency=2)
    agent.ask("collaborate via blackboard")

    bb_path = agent.current_run_dir / "blackboard.json"
    assert bb_path.exists()
    data = json.loads(bb_path.read_text(encoding="utf-8"))
    assert any(n["key"] == "api.sig" and n["from_task"] == "t1" for n in data["notes"])
    # t2 的 prompt 里应出现读到的黑板内容
    board = BoardStore(agent.current_run_dir).load()
    assert board.task("t1")["status"] == "done" and board.task("t2")["status"] == "done"


# ---------------- 端到端：wave 模式 + 进度摘要 ----------------

def test_run_tasks_wave_returns_digest_and_one_wave(tmp_path):
    scripts = [['<final>{"status":"done","summary":"ok"}</final>'] for _ in range(3)]
    state = {"i": 0}

    def factory():
        s = scripts[min(state["i"], len(scripts) - 1)]
        state["i"] += 1
        return FakeModelClient(list(s))

    # t1 无依赖；t2 依赖 t1。wave 模式下第一次 run_tasks 只跑 t1。
    tasks = [
        {"task_id": "t1", "role": "explorer", "goal": "a"},
        {"task_id": "t2", "role": "explorer", "goal": "b", "depends_on": ["t1"]},
    ]
    outputs = [
        _tool("plan_tasks", {"tasks": tasks}),
        _tool("run_tasks", {"task_ids": "ready", "mode": "wave"}),
        "<final>stopped</final>",
    ]
    agent = _parent(tmp_path, outputs, factory, max_concurrency=3)
    agent.ask("one wave only")
    board = BoardStore(agent.current_run_dir).load()
    assert board.task("t1")["status"] == "done"
    assert board.task("t2")["status"] == "pending"   # 第二波没跑
    # 进度摘要出现在 run_tasks 的返回里（作为工具结果写进历史）
    tool_events = [i for i in agent.session["history"] if i["role"] == "tool" and i["name"] == "run_tasks"]
    assert tool_events and "progress digest" in tool_events[0]["content"]
    assert "ready to run next: t2" in tool_events[0]["content"]


# ---------------- 端到端：blocked -> answer_need -> 重试完成 ----------------

def test_blocked_then_answer_need_then_complete(tmp_path):
    # 第一次子 agent 自报 blocked+needs；answer_need 后重试返回 done
    scripts = [
        ['<final>{"status":"blocked","summary":"need the key","needs":[{"question":"which config key?"}]}</final>'],
        ['<final>{"status":"done","summary":"used the answer"}</final>'],
    ]
    state = {"i": 0}

    def factory():
        s = scripts[min(state["i"], len(scripts) - 1)]
        state["i"] += 1
        return FakeModelClient(list(s))

    tasks = [{"task_id": "t1", "role": "explorer", "goal": "look up key", "max_attempts": 1}]
    outputs = [
        _tool("plan_tasks", {"tasks": tasks}),
        _tool("run_tasks", {"task_ids": "ready"}),           # -> blocked
        _tool("answer_need", {"task_id": "t1", "text": "the key is api_base"}),
        _tool("run_tasks", {"task_ids": ["t1"]}),            # retry -> done
        "<final>done</final>",
    ]
    agent = _parent(tmp_path, outputs, factory, max_concurrency=1)
    agent.ask("blocked then answered")
    board = BoardStore(agent.current_run_dir).load()
    assert board.task("t1")["status"] == "done"
    assert any("api_base" in f for f in board.task("t1")["inputs"]["facts"])
    trace = agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8")
    assert "task_blocked" in trace and "need_answered" in trace


# ---------------- 端到端：cancel_tasks 止损 ----------------

def test_cancel_tasks_stops_pending_and_cascades(tmp_path):
    scripts = [['<final>{"status":"done","summary":"ok"}</final>']]
    state = {"i": 0}

    def factory():
        state["i"] += 1
        return FakeModelClient(list(scripts[0]))

    tasks = [
        {"task_id": "t1", "role": "explorer", "goal": "a"},
        {"task_id": "t2", "role": "explorer", "goal": "b", "depends_on": ["t1"]},
    ]
    outputs = [
        _tool("plan_tasks", {"tasks": tasks}),
        _tool("cancel_tasks", {"task_ids": ["t1"], "reason": "approach wrong"}),
        "<final>stopped</final>",
    ]
    agent = _parent(tmp_path, outputs, factory, max_concurrency=1)
    agent.ask("cancel before running")
    board = BoardStore(agent.current_run_dir).load()
    assert board.task("t1")["status"] == STATUS_CANCELLED
    assert board.task("t2")["status"] == "blocked"
    trace = agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8")
    assert "task_cancelled" in trace
