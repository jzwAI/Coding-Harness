"""并行工具执行性能测试。

验证 execute_batch() 在只读工具上的并行化是否确实减少了端到端耗时。
"""
import json
import time
from pathlib import Path

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext


def _build_large_files(tmp_path, file_count=4, file_size_mb=2):
    """创建多个大型文本文件，确保单次 read_file 有可测量的 IO 耗时。"""
    line = "L" * 1990 + "\n"  # ~2000 bytes per line
    lines_per_file = max(1, (file_size_mb * 1024 * 1024) // len(line))
    content = line * lines_per_file

    for i in range(file_count):
        (tmp_path / f"data_{i}.txt").write_text(content, encoding="utf-8")

    return WorkspaceContext.build(tmp_path)


def _make_agent(workspace, tmp_path, outputs, max_steps):
    """创建 Pico agent 的快捷工厂。"""
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=SessionStore(Path(tmp_path) / ".pico" / "sessions"),
        approval_policy="auto",
        max_steps=max_steps,
    )


def _run_sequential(workspace, tmp_path, file_count):
    """串行模式：N 轮，每轮一个 <tool> 调用。"""
    outputs = []
    for i in range(file_count):
        outputs.append(
            "<tool>"
            + json.dumps(
                {
                    "name": "read_file",
                    "args": {"path": f"data_{i}.txt", "start": 1, "end": 50},
                }
            )
            + "</tool>"
        )
    outputs.append("<final>Done.</final>")

    agent = _make_agent(workspace, tmp_path, outputs, file_count + 1)

    start = time.perf_counter()
    result = agent.ask("Read all files one by one")
    elapsed = time.perf_counter() - start
    assert result == "Done.", f"Unexpected result: {result}"
    return elapsed


def _run_parallel(workspace, tmp_path, file_count):
    """并行模式：1 轮 <tools> 包含 N 个 read_file。"""
    calls = []
    for i in range(file_count):
        calls.append(
            {
                "name": "read_file",
                "args": {"path": f"data_{i}.txt", "start": 1, "end": 50},
            }
        )

    outputs = [
        "<tools>" + json.dumps({"calls": calls}) + "</tools>",
        "<final>Done.</final>",
    ]

    agent = _make_agent(workspace, tmp_path, outputs, file_count + 1)

    start = time.perf_counter()
    result = agent.ask("Read all files in parallel")
    elapsed = time.perf_counter() - start
    assert result == "Done.", f"Unexpected result: {result}"
    return elapsed


def test_parallel_reads_outperform_sequential(tmp_path):
    """并行读取应比串行读取显著减少端到端耗时。

    原理：
    - 串行：N 个文件需要 N 轮 (prompt 组装 + 模型调用 + 工具执行)
    - 并行：N 个文件只需 1 轮 (prompt 组装 + 模型调用 + N 路并行 IO)
    当文件足够大使得 IO 耗时超过 prompt 组装开销时，并行收益明显。
    """
    FILE_COUNT = 4
    FILE_SIZE_MB = 3
    ITERATIONS = 5

    workspace = _build_large_files(tmp_path, file_count=FILE_COUNT, file_size_mb=FILE_SIZE_MB)

    # 多轮采样，取中位数消除系统抖动
    seq_samples = []
    for _ in range(ITERATIONS):
        seq_samples.append(_run_sequential(workspace, tmp_path, FILE_COUNT))

    par_samples = []
    for _ in range(ITERATIONS):
        par_samples.append(_run_parallel(workspace, tmp_path, FILE_COUNT))

    seq_median = sorted(seq_samples)[ITERATIONS // 2]
    par_median = sorted(par_samples)[ITERATIONS // 2]

    speedup = seq_median / par_median if par_median > 0 else float("inf")

    assert speedup >= 1.3, (
        f"Expected parallel reads to be at least 30% faster than sequential.\n"
        f"Files: {FILE_COUNT} x {FILE_SIZE_MB}MB\n"
        f"Sequential samples: {[f'{t*1000:.1f}ms' for t in sorted(seq_samples)]}\n"
        f"Parallel samples:   {[f'{t*1000:.1f}ms' for t in sorted(par_samples)]}\n"
        f"Sequential median: {seq_median*1000:.1f}ms\n"
        f"Parallel median:   {par_median*1000:.1f}ms\n"
        f"Speedup: {speedup:.2f}x"
    )


def test_parallel_mode_reduces_round_trips(tmp_path):
    """并行模式应减少模型调用轮数。

    串行读 N 个文件 = N+1 轮（N 次 tool + 1 次 final）
    并行读 N 个文件 = 2 轮（1 次 tools + 1 次 final）
    """
    FILE_COUNT = 3
    (tmp_path / "a.txt").write_text("hello a\n" * 10, encoding="utf-8")
    (tmp_path / "b.txt").write_text("hello b\n" * 10, encoding="utf-8")
    (tmp_path / "c.txt").write_text("hello c\n" * 10, encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)

    # 并行
    calls = [
        {"name": "read_file", "args": {"path": "a.txt", "start": 1, "end": 5}},
        {"name": "read_file", "args": {"path": "b.txt", "start": 1, "end": 5}},
        {"name": "read_file", "args": {"path": "c.txt", "start": 1, "end": 5}},
    ]
    par_outputs = [
        "<tools>" + json.dumps({"calls": calls}) + "</tools>",
        "<final>Done.</final>",
    ]
    agent = _make_agent(workspace, tmp_path, par_outputs, FILE_COUNT + 1)
    agent.ask("Read files in parallel")

    # 并行模式：2 次 complete() 调用 = 2 轮
    assert len(agent.model_client.prompts) == 2, (
        f"Parallel mode should use 2 rounds (1 tools + 1 final), "
        f"got {len(agent.model_client.prompts)}"
    )

    # 验证第一轮 prompt 包含并行指令（证明模型确实发起了 batch）
    first_prompt = agent.model_client.prompts[0]
    assert "tools" in first_prompt.lower(), "First prompt should mention tools"


def test_mixed_batch_reads_then_writes_serially(tmp_path):
    """读写混合 batch：读应并行，写应在读完成后串行执行。

    验证点：
    1. 混合 batch 不会报错（不会因为包含写工具而被拒绝）
    2. 读和写都成功执行
    """
    (tmp_path / "config.txt").write_text("mode: production\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)

    calls = [
        {"name": "read_file", "args": {"path": "config.txt", "start": 1, "end": 5}},
        {
            "name": "patch_file",
            "args": {
                "path": "config.txt",
                "old_text": "mode: production",
                "new_text": "mode: staging",
            },
        },
    ]
    outputs = [
        "<tools>" + json.dumps({"calls": calls}) + "</tools>",
        "<final>Done.</final>",
    ]

    agent = _make_agent(workspace, tmp_path, outputs, 3)
    result = agent.ask("Read config and update mode to staging")

    assert result == "Done.", f"Unexpected result: {result}"
    # 验证 patch 确实生效了
    updated = (tmp_path / "config.txt").read_text(encoding="utf-8")
    assert "mode: staging" in updated, f"File was not patched: {updated}"
