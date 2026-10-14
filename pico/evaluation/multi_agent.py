"""多 agent 协作评测层（FakeModelClient 剧本版）。

用确定性的假模型跑一个"规划 → 并行 worker → 汇总"的场景，产出可复现的指标：
任务成功率、加速比代理、各状态计数、冲突与越界次数。真实模型版留待接入 provider 后补。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from ..orchestration.metrics import summarize_board
from ..orchestration.task_board import BoardStore
from ..providers.clients import FakeModelClient
from ..runtime import Pico, SessionStore
from ..workspace import WorkspaceContext


def _tool(name, args):
    return "<tool>" + json.dumps({"name": name, "args": args}) + "</tool>"


def run_scripted_multi_agent_scenario(max_concurrency=3):
    """跑一个固定的三 worker 并行场景，返回 (summary, board_dict)。

    不依赖真实模型、不依赖 git：worker 直接写主工作区（串行回退路径），
    用于回归验证编排层的端到端行为与指标汇总。
    """
    with tempfile.TemporaryDirectory(prefix="pico-ma-eval-") as temp_dir:
        root = Path(temp_dir)
        (root / "README.md").write_text("demo\n", encoding="utf-8")
        workspace = WorkspaceContext.build(root)
        store = SessionStore(root / ".pico" / "sessions")

        child_scripts = [
            ['<final>{"status":"done","summary":"explored"}</final>'],
            ['<tool name="write_file" path="a.txt"><content>a\n</content></tool>',
             '<final>{"status":"done","summary":"wrote a","changed_files":["a.txt"]}</final>'],
            ['<tool name="write_file" path="b.txt"><content>b\n</content></tool>',
             '<final>{"status":"done","summary":"wrote b","changed_files":["b.txt"]}</final>'],
        ]
        state = {"i": 0}

        def factory():
            script = child_scripts[min(state["i"], len(child_scripts) - 1)]
            state["i"] += 1
            return FakeModelClient(list(script))

        tasks = [
            {"task_id": "t1", "role": "explorer", "goal": "inspect"},
            {"task_id": "t2", "role": "worker", "goal": "write a.txt", "depends_on": ["t1"], "write_scope": ["a.txt"]},
            {"task_id": "t3", "role": "worker", "goal": "write b.txt", "depends_on": ["t1"], "write_scope": ["b.txt"]},
        ]
        outputs = [
            _tool("plan_tasks", {"tasks": tasks}),
            _tool("run_tasks", {"task_ids": "ready"}),   # t1
            _tool("run_tasks", {"task_ids": "ready"}),   # t2, t3 parallel
            "<final>done</final>",
        ]
        agent = Pico(
            model_client=FakeModelClient(outputs),
            workspace=workspace,
            session_store=store,
            approval_policy="auto",
            max_steps=8,
            model_client_factory=factory,
            orchestration_config={"max_concurrency": max_concurrency},
        )
        agent.ask("build files with sub-agents")
        board = BoardStore(agent.current_run_dir).load()
        summary = summarize_board(board)
        return summary, board.to_dict()
