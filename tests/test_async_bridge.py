"""v3.0 + v3.1 异步基础设施测试：桥接层 + provider 的 complete_async。

高风险的核心异步化（agent_loop / tool_executor / orchestrator）在后续步骤实现，
每步单独验证。这里只覆盖已落地的桥接层与 provider 异步面。
"""

import asyncio
import threading
import time

from pico.aio import in_thread, run_sync, first_or_cancel
from pico import FakeModelClient


# ---------------- 桥接层 ----------------

def test_run_sync_drives_coroutine():
    async def add(a, b):
        await asyncio.sleep(0)
        return a + b
    assert run_sync(add(2, 3)) == 5


def test_run_sync_works_inside_running_loop():
    async def add(a, b):
        return a + b

    async def outer():
        return run_sync(add(1, 1))   # 在运行中的循环里调用 run_sync 不应报错
    assert run_sync(outer()) == 2


def test_in_thread_runs_blocking_concurrently():
    def blocking(x):
        time.sleep(0.2)
        return x * 10

    async def parallel():
        start = time.monotonic()
        res = await asyncio.gather(in_thread(blocking, 1), in_thread(blocking, 2), in_thread(blocking, 3))
        return res, time.monotonic() - start

    res, elapsed = run_sync(parallel())
    assert res == [10, 20, 30]
    assert elapsed < 0.45   # 并发而非串行（串行需 ~0.6s）


def test_first_or_cancel_normal_and_cancel():
    async def slow_ok():
        await asyncio.sleep(0.05)
        return "done"

    # 正常完成
    r, cancelled = run_sync(first_or_cancel(slow_ok(), threading.Event()))
    assert r == "done" and cancelled is False

    # 取消抢先
    async def scenario():
        ev = threading.Event()

        async def slow():
            await asyncio.sleep(5)
            return "nope"

        async def trigger():
            await asyncio.sleep(0.05)
            ev.set()

        tg = asyncio.ensure_future(trigger())
        out = await first_or_cancel(slow(), ev)
        await tg
        return out

    r, cancelled = run_sync(scenario())
    assert r is None and cancelled is True


def test_first_or_cancel_without_event():
    async def slow_ok():
        await asyncio.sleep(0.01)
        return "done"
    r, cancelled = run_sync(first_or_cancel(slow_ok(), None))
    assert r == "done" and cancelled is False


# ---------------- provider 异步面 ----------------

def test_fake_model_client_complete_async_matches_sync():
    client = FakeModelClient(["<final>one</final>", "<final>two</final>"])

    async def go():
        a = await client.complete_async("p1", 100)
        b = await client.complete_async("p2", 100)
        return a, b

    a, b = run_sync(go())
    assert a == "<final>one</final>" and b == "<final>two</final>"
    assert client.prompts == ["p1", "p2"]


def test_fake_model_client_async_accepts_cache_kwargs():
    client = FakeModelClient(["<final>ok</final>"])

    async def go():
        return await client.complete_async("p", 50, prompt_cache_key="k", prompt_cache_retention="in_memory")

    assert run_sync(go()) == "<final>ok</final>"


def test_real_clients_expose_complete_async():
    # 不发网络请求，只验证异步接口存在且可调用（签名正确）。
    from pico import OllamaModelClient, OpenAICompatibleModelClient, AnthropicCompatibleModelClient
    import inspect
    for cls in (OllamaModelClient, OpenAICompatibleModelClient, AnthropicCompatibleModelClient):
        assert hasattr(cls, "complete_async")
        assert inspect.iscoroutinefunction(cls.complete_async)
