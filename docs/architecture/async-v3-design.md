# pico 异步事件循环重构设计 v3

> 状态：设计稿，尚未实现。本文件把"要改哪些地方、每处怎么改"逐一设计到位，作为分步实现的依据。
> 目标：把 pico 从"同步阻塞"改成"asyncio 事件循环驱动"，解锁任务进行中介入、组员实时协作、流式输出与打断。
> 硬约束不变：**零第三方依赖**、Windows/Linux/macOS 都能跑、现有 180+ 测试不破。

---

## 0. 为什么要改、改完解锁什么

| 现在（同步阻塞） | 改后（asyncio） |
|---|---|
| 单个 agent 等模型回复时整个程序卡住 | 等模型时事件循环可推进别的协程 |
| 组员之间只能异步留言（黑板/信箱），不能实时问答 | 可以 `await` 一条消息，实现"问—答"而不阻塞全局 |
| 组长只能在"批次之间"介入 | 可在任务运行中介入（取消是即时的，不必等自然结束） |
| 工具执行期间 agent loop 完全卡住 | 工具在 executor 里跑，loop 可同时处理打断信号 |
| 无流式输出 | 可边收 SSE 边产出、随时打断 |

---

## 1. 根本约束：零依赖下如何做异步 HTTP

Python 标准库没有异步 HTTP 客户端。决策：**保留同步 `urllib`/`subprocess`，用 `loop.run_in_executor` 丢进线程池**，让它们在不阻塞事件循环的前提下并发执行。

- 事件循环是真异步的；只有"叶子级阻塞 IO"（模型 HTTP、子进程、文件）被委派到 executor 线程。
- 零依赖保住；`clients.py`/`tools.py` 里的 HTTP、subprocess 代码几乎原样保留，只是从"直接调用"变成"在 executor 里被调用"。
- `asyncio.to_thread(fn, *args)`（Python 3.9+，stdlib）是最简封装；pico 要求 3.10+，可直接用。

```python
# 统一的阻塞→异步桥
async def _in_thread(fn, *args, **kwargs):
    import asyncio, functools
    return await asyncio.to_thread(functools.partial(fn, *args, **kwargs))
```

---

## 2. 分层改造策略：async 核心 + 同步包装器（关键兼容手段）

**不把同步 API 删掉**，而是：每个核心方法新增 `_async` 版承载真逻辑，旧的同步名保留为薄包装器，内部用 `asyncio.run` 或"复用当前循环"驱动。

```python
def ask(self, user_message):
    # 同步入口：供 CLI one-shot、测试、评测、以及旧调用方使用。
    return _run_sync(self.ask_async(user_message))

async def ask_async(self, user_message):
    return await AgentLoop(self).run_async(user_message)
```

`_run_sync` 的实现要稳健处理"已在事件循环里"的情况：

```python
def _run_sync(coro):
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)          # 没有运行中的循环：正常同步入口
    # 已在循环里（不应发生于同步调用方）——用独立线程跑一个新循环，避免 "loop already running"
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()
```

**收益**：18 处 `.ask()` 调用（测试、评测、`cli.py`、`spawn_delegate`）一行不改即可继续工作；新代码走 `ask_async` 拿到异步能力。180 个测试的兼容性由此保证。

---

## 3. 逐文件改造清单

### 3.1 `pico/providers/clients.py` — 模型后端（核心）

**现状**：`FakeModelClient` / `OllamaModelClient` / `OpenAICompatibleModelClient` / `AnthropicCompatibleModelClient` 各有同步 `complete()`，内部 `urllib.request.urlopen` + `time.sleep` 重试。

**改法**：
- 每个 client 新增 `async def complete_async(...)`；旧 `complete()` 保留为 `_run_sync(self.complete_async(...))` 包装。
- `complete_async` 内部：把 `urlopen` 那段同步代码整体包进 `await _in_thread(self._complete_blocking, ...)`。即把现有函数体改名为 `_complete_blocking` 原样保留，`complete_async` 只负责 `await _in_thread(...)`。
- `time.sleep(x)` 重试退避 → `await asyncio.sleep(x)`（移出阻塞段，放在 `complete_async` 层循环）。
- `FakeModelClient.complete_async` 直接返回脚本输出（无 IO，不需 executor），但要 `async def`，这样测试里的 async 路径也能 await 它。
- `supports_prompt_cache`、`last_completion_metadata` 等属性语义不变。

**签名**：`async def complete_async(self, prompt, max_new_tokens, *, prompt_cache_key=None, prompt_cache_retention=None) -> str`

**风险**：`last_completion_metadata` 是实例属性，并发复用同一 client 会串（v1 已通过 `model_client_factory` 给每个子 agent 独立实例解决）。异步并发同一实例仍不安全——文档强调：**每个并发 agent 必须用工厂建独立 client**，这条在 async 下从"建议"升级为"硬性要求"。

### 3.2 `pico/agent_loop.py` — 主循环（核心）

**现状**：`AgentLoop.run(user_message)` 是同步 while 循环：组 prompt → `model_client.complete` → parse → `execute_tool` → trace。

**改法**：
- 新增 `async def run_async(self, user_message)`，把现有 `run` 的逻辑搬进去。
- `raw = agent.model_client.complete(...)` → `raw = await agent.model_client.complete_async(...)`。
- 工具执行 `agent.execute_tool(...)` → `await agent.execute_tool_async(...)`（见 3.3）。
- 并行只读批 `execute_batch` → `await execute_batch_async`（见 3.3）。
- **取消检查从"每轮开头"升级为"每个 await 点可感知"**：保留开头的 `cancel_event.is_set()` 快速退出；此外，因为模型调用现在是 `await`，可以用 `asyncio.wait` + 一个"取消 waiter"实现**运行中打断**——模型还没回来就能停。
- 旧 `run()` 保留为 `_run_sync(self.run_async(...))` 包装，供同步 `ask()` 用。

**新增：运行中打断的实现**
```python
model_task = asyncio.ensure_future(agent.model_client.complete_async(...))
cancel_task = asyncio.ensure_future(_wait_cancel(agent.cancel_event))
done, pending = await asyncio.wait({model_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
if cancel_task in done:
    model_task.cancel()
    # 收尾：标记 cancelled、写 trace、返回
```
这是同步模型下**做不到**、异步后才解锁的能力。

### 3.3 `pico/runtime.py` — Pico 外观层（核心）

**现状**：`ask` / `execute_tool` / `run_tool` / `approve` / `spawn_delegate` 均同步。

**改法**：
- `ask` → 保留同步包装；新增 `ask_async`（见 §2）。
- `execute_tool(name, args)` → 新增 `async def execute_tool_async`；内部 `await self.tool_executor.execute_async(name, args)`。同步 `execute_tool` 保留为包装（延迟导入 `_run_sync`），供任何残留同步调用方。
- `approve(name, args)`：审批可能要等人输入。新增 `async def approve_async`：
  - `auto`/`never`/`read_only` 分支即时返回，不需 await。
  - `ask` 分支：`await _in_thread(input, prompt)`（把阻塞的 `input()` 丢进 executor），或走 `ApprovalBroker` 的异步版（见 3.8）。
- `spawn_delegate(args)`：`child.ask(task)` → `await child.ask_async(task)`；`spawn_delegate` 本身改 `async def spawn_delegate_async`，旧名保留包装。注意 `tool_delegate` 工具的 `run` 回调最终由 executor 调用链触达（见 3.4 工具执行模型）。
- `cancel_event`：已是 `threading.Event`，在 async 下仍可用（`is_set()` 非阻塞）；新增一个 `asyncio.Event` 镜像用于 `await` 唤醒，或用轮询式 `_wait_cancel`。

### 3.4 `pico/tool_executor.py` — 工具执行（核心）

**现状**：`execute()` 同步跑工具；`execute_batch()` 用 `ThreadPoolExecutor` 并行只读、串行写。

**改法**：
- 新增 `async def execute_async(name, args)`：护栏逻辑（白名单/校验/重复/审批/快照）大多是 CPU/内存操作，可留同步；只有两处要 await：
  - `approve` → `await agent.approve_async(...)`。
  - 真正的 `tool["run"](args)`（可能是 shell/文件 IO/子 agent）→ `await _in_thread(tool["run"], args)`；若该工具是 `delegate`，其 `run` 内部会触发子 agent，需要走异步委派链（见下）。
- `execute_batch()` → `async def execute_batch_async`：用 `asyncio.gather` 替代 `ThreadPoolExecutor` 跑并行只读；写操作仍 `await` 串行。并发上限用 `asyncio.Semaphore(MAX_READ_WORKERS)` 控制。
- **delegate 的异步化**：`delegate` 工具的 `run` 目前是 `context.spawn_delegate(args)`（同步 `child.ask`）。改为：`ToolContext` 增加 `spawn_delegate_async`，执行器在工具名为 `delegate` 时走 `await context.spawn_delegate_async(args)`，而不是丢 executor（否则会在 executor 线程里再起事件循环）。
- 旧 `execute`/`execute_batch` 保留为同步包装。

**要点**：`run_in_executor` 适合"叶子阻塞"（HTTP、subprocess、file）；**不要**把"内部还会 await 的东西"丢进 executor，否则线程里得再建循环。delegate / 子 agent 这类必须留在主事件循环用 `await`。

### 3.5 `pico/orchestration/orchestrator.py` — 并行调度（核心）

**现状**：`_schedule` 用 `ThreadPoolExecutor` 并行跑子 agent（每个 `_execute_attempt` 内部 `child.ask`）。

**改法**：
- `_execute_attempt` → `async def _execute_attempt_async`：`child.ask(brief)` → `await child.ask_async(brief)`。
- `_schedule` 的并行从 `ThreadPoolExecutor` → `asyncio.gather` + `asyncio.Semaphore(concurrency)`。
- `run_tasks` → 保留同步包装 + `run_tasks_async`。
- **git worktree 的串行锁** `self._git_lock`（threading.Lock）→ 换 `asyncio.Lock`；因为现在 worker 协程并发,共享 `.git` 的 worktree/commit 仍需串行,但要用异步锁避免阻塞循环。worktree 的 git 子进程本身走 `await _in_thread`。
- 黑板写入锁、ApprovalBroker 锁同理评估（见 3.8）。
- **真正的运行中介入**：异步后，组长协程可以在 worker 协程运行期间收到用户消息（经由一个 `asyncio.Queue`），立即 `cancel_tasks` 并 `cancel_event.set()`，worker 的 `await` 点会即时抛 `CancelledError` 停下——这就是 v2 文档里"做不到"、v3 才解锁的能力。

### 3.6 `pico/cli.py` — 入口与 REPL

**现状**：`main()` 同步；REPL 用 `input()` 阻塞读。

**改法**：
- `main()` 保留为同步，内部 `asyncio.run(main_async(args))`。
- `main_async`：one-shot 走 `await agent.ask_async(prompt)`。
- REPL：`user_input = await _in_thread(input, "pico> ")`,这样读用户输入时事件循环仍活着,可以同时推进后台任务(未来的"边跑边聊")。v3.1 可先保持简单的 `await ask_async`,v3.2 再加"输入与任务并发"。
- 新增 `--stream` 开关(配合 3.1 的流式),one-shot 下边收边打印。

### 3.7 `pico/tools.py` / `workspace.py` / `verify.py` / `worktree.py` / `integrator.py` — 子进程与文件

**现状**：全部 `subprocess.run` 同步 + 文件 IO。

**改法**：
- 这些是"叶子阻塞",**本身不改**(保持同步函数),由调用方用 `await _in_thread(...)` 包。
- 唯一例外：`tool_run_shell` 的 `subprocess.run(..., timeout=t)` 已有超时;异步后超时仍由 subprocess 自身保证,executor 线程会在子进程结束后归还。
- `workspace.py` 的 git 探测、`capture_workspace_snapshot` 的 rglob+sha256 是中等开销的阻塞操作:在热路径上(每次工具执行前后快照)建议也 `await _in_thread`,避免大仓库下卡住循环。

### 3.8 `pico/orchestration/approval.py` / `blackboard.py` — 协作原语

- `ApprovalBroker`:`threading.Lock` → 可保留(非阻塞获取很快),但 `input()` 要 `await _in_thread`。更干净的做法:新增 `async def request_async`,内部 `await _in_thread(self._prompt, msg)`,并用 `asyncio.Lock` 串行化并发审批。
- `Blackboard`:`post/read/send/inbox` 都是内存操作 + 小文件原子写,极快,**可保持同步**;在协程里直接调用不会明显卡循环。若要极致,可把 `_persist` 的文件写 `await _in_thread`。结论:v3 阶段黑板**不强制改**,降低改动面。

### 3.9 测试与评测 `tests/` `evaluation/`

- 因为同步 `ask()` 等入口全部保留,**现有测试默认不动**。
- 新增 `tests/test_async_*.py`:直接 await `ask_async` / `run_tasks_async` / `complete_async`,验证异步路径与"运行中取消"。
- `FakeModelClient` 需要 `complete_async`;给它加一个 `async def complete_async` 直接返回脚本项即可,同步 `complete` 保留。
- 评测里的 `agent.ask(...)` 不变。

---

## 4. 事件循环所有权与边界（最容易踩坑处）

1. **一个请求 = 一个事件循环**:同步 `ask()` 用 `asyncio.run` 开一个循环跑完即关。不要跨请求复用循环。
2. **禁止在 executor 线程里 await**:executor 跑的必须是纯同步叶子函数。凡是"内部还要调度协程"的(delegate、子 agent、并行调度),留在主循环 `await`。
3. **CPU 密集仍受 GIL**:`context_manager` 组 prompt、sha256 快照是 CPU 活;异步不会让它们并行,但把它们 `await _in_thread` 至少不卡住 IO 协程。热路径按需处理。
4. **取消语义**:`cancel_event`(threading)用于跨线程只读检查;另加 `asyncio.Event` 或 `_wait_cancel` 协程用于主循环 `await` 唤醒。两者在 `cancel_tasks` 里一起 set。
5. **线程安全没有消失**:executor 仍是多线程,v1 的 `_state_lock`(memory/session 写)、`_git_lock`、黑板锁**全部保留**。异步只是换了调度方式,共享可变状态该加锁还得加。

---

## 5. 分步实现与验证门（每步都要你跑 pytest）

| 步 | 内容 | 风险 | 验证重点 |
|---|---|---|---|
| **v3.0 桥接层** | 加 `_in_thread`、`_run_sync` 工具函数 + 单测 | 低 | 新工具函数单测;全套回归仍绿 |
| **v3.1 providers 异步** | 各 client 加 `complete_async`(executor 包),`FakeModelClient.complete_async`;同步 `complete` 保留 | 中 | 现有 provider 测试绿;新增 async client 测试 |
| **v3.2 agent_loop 异步** | `run_async` + 同步 `run` 包装;`ask_async`/`ask` | 高 | **全套 180+ 必须全绿**(同步包装正确性) |
| **v3.3 工具执行异步** | `execute_async`/`execute_batch_async`;delegate 异步链 | 高 | 并行只读、delegate、安全不变量测试 |
| **v3.4 编排异步** | `_schedule`→gather+Semaphore;`_git_lock`→asyncio.Lock;`run_tasks_async` | 高 | v1/v2 编排全套绿;新增"运行中取消"测试 |
| **v3.5 CLI 异步** | `main_async`;REPL `input` 入 executor | 中 | one-shot/REPL 手测 + 冒烟 |
| **v3.6 运行中介入** | 组长协程 + `asyncio.Queue` 接用户消息;worker 运行中可被 cancel | 高 | 新增:worker 跑到一半被 cancel,即时停 |
| **v3.7 流式(可选)** | `complete_async` 产出增量;`--stream`;打断 | 中 | 流式输出 + Esc 打断手测 |

每步独立合入、独立验证;任何一步红就停下修,不往上叠。

---

## 6. 风险与取舍

- **工作量**:这是全链路改造,涉及约 8 个核心文件的签名与调用链。即便有同步包装兜底,v3.2/v3.3/v3.4 三步每步都可能牵出 await 遗漏、executor 里误 await、锁与循环交互的问题——而这些 pytest 才能暴露。
- **零依赖的代价**:用 executor 包同步 IO,拿到的是"并发"而非"真异步 IO";对 pico(IO 密集、并发度不高)完全够用,但要诚实:这不是 aiohttp 那种原生异步栈。
- **复杂度上升**:async/sync 双轨会让代码变难读,与 pico"简单可读"的定位有张力。建议:核心走 async,叶子保持同步,双轨只存在于少数外观方法。
- **收益是否值得**:只有当你真的要"运行中介入 / 实时协作 / 流式打断"时才值。若只是并行跑子 agent,v1 的线程池已经够(模型调用是 IO,线程并发有效)。**这一点必须对面试官讲清楚:我做了异步,是为了解锁 X,而不是因为"异步更高级"。**

---

## 7. 一句话总结

> 用 **asyncio 事件循环 + `run_in_executor` 包同步 IO** 实现异步(零依赖保住);核心方法走 `_async`,旧同步名保留为 `_run_sync` 包装(180 测试不破);真正解锁的是"运行中介入、实时协作、流式打断"——这些是同步模型下做不到的。分 8 步实现,每步过 pytest。
