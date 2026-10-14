"""异步桥接工具：在零依赖前提下把同步阻塞 IO 接进 asyncio 事件循环。

设计见 docs/architecture/async-v3-design.md。两条核心工具：
- `in_thread(fn, *a, **kw)`：把一个同步阻塞函数（HTTP、subprocess、文件）丢到线程池里
  执行，`await` 它而不阻塞事件循环。只能用于"纯叶子"同步函数——内部还要调度协程的
  东西（子 agent、并行调度）必须留在主循环直接 await。
- `run_sync(coro)`：从同步代码驱动一个协程跑完并拿到结果。供保留的同步外观方法
  （ask/complete 等）包装异步实现用，使 180+ 现有测试与评测无需改动。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools


async def in_thread(fn, *args, **kwargs):
    """在默认线程池里执行同步函数 fn，返回其结果（可 await，不阻塞事件循环）。"""
    if kwargs:
        fn = functools.partial(fn, **kwargs)
    return await asyncio.to_thread(fn, *args)


def run_sync(coro):
    """同步驱动一个协程跑完。

    - 当前线程没有运行中的事件循环：直接 asyncio.run（正常同步入口）。
    - 已经在事件循环里（同步方法被异步上下文误调用）：在独立线程里开新循环跑，
      避免 "asyncio.run() cannot be called from a running event loop"。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


async def wait_cancel(cancel_event, poll_interval=0.05):
    """等待一个 threading.Event 被 set；用于在主循环里与模型调用一起 await，
    实现"运行中打断"。返回时表示已被取消。"""
    while not cancel_event.is_set():
        await asyncio.sleep(poll_interval)
    return True


async def first_or_cancel(coro, cancel_event):
    """并发等待 coro 与取消信号；谁先到返回谁。

    返回 (result, cancelled)：
    - 正常完成：(coro 的返回值, False)
    - 被取消：(None, True)，并尽力取消 coro。
    """
    main_task = asyncio.ensure_future(coro)
    if cancel_event is None:
        return await main_task, False
    cancel_task = asyncio.ensure_future(wait_cancel(cancel_event))
    try:
        done, pending = await asyncio.wait(
            {main_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if main_task in done:
            cancel_task.cancel()
            return main_task.result(), False
        # 取消先到：中止主任务
        main_task.cancel()
        try:
            await main_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        return None, True
    finally:
        for task in (main_task, cancel_task):
            if not task.done():
                task.cancel()
