# pico 多 Agent 协作设计 v1

> 状态：已实现（Phase 0–4）。实际落地与本稿的差异见第 14 节"实施路线"表。
> 前提：保持 pico 现有的边界——单进程、零第三方依赖、同步 IO、文本工具协议，Windows / Linux / macOS 都能跑。

## 阅读指南

每一节分三块：

1. **设计**：给实现的人看，写清楚怎么做。
2. **💬 白话解释**：用日常语言说明这一节在讲什么、为什么要这样。
3. **📖 本节术语**：这一节里出现的专业词逐个解释。

所有术语汇总在文末的**附录 A**，可以当词典查。

---

## 0. 目标与非目标

**目标**：主 agent（Orchestrator）把一个大任务拆给多个受限的子 agent，能并行的就并行执行，最后结果能**合并、验证、复盘、评测**。

**非目标（v1 不做）**：
- 跨进程或跨机器的分布式执行
- 子 agent 之间自由对话（网状通信）
- 流式输出、中途打断
- 无限层级的递归委派

> 💬 **白话解释**
> 现在的 pico 是"一个人干所有活"。这份设计要把它变成"一个组长带几个组员"：组长负责拆任务、分配、检查和汇总；组员各干一小块，互不影响的活可以同时干。干完之后能把大家的成果合到一起，能自动检查对不对，能事后查清楚谁做了什么，还能用数据衡量"多人协作"到底比"一个人干"好在哪。
> "非目标"是这一版**刻意不做**的事，免得一次做得太大、做不完。比如组员之间不能私下聊天，有事只能通过组长；组员也不能再招自己的下属。

> 📖 **本节术语**
> - **agent**：能自己"看情况 → 决定做什么 → 调用工具去做 → 看结果"循环工作的程序，大脑是大模型。
> - **Orchestrator（编排者）**：总负责的那个主 agent，相当于组长。
> - **子 agent**：被组长派出去干某一小块活的 agent，相当于组员。
> - **并行**：多件事在同一段时间里同时进行，相对的是"串行"（一件做完再做下一件）。
> - **复盘**：事后回看整个过程，搞清楚每一步发生了什么。
> - **评测**：用固定的题目和指标衡量系统好不好。
> - **进程**：操作系统里一个正在运行的程序。"单进程"指所有 agent 都在同一个 Python 程序里跑。
> - **分布式**：把工作分到多个进程甚至多台机器上执行。
> - **同步 IO**：发出请求（比如调用模型 API）后原地等结果回来，再往下走。
> - **流式输出**：模型边生成边显示，像打字一样，而不是全部生成完才一次性给出。
> - **递归委派**：组员再派自己的组员，组员的组员再往下派……层层嵌套。

---

## 1. 现状与边界（基于当前代码）

现有的 `delegate` 工具（`tools.py` + `runtime.spawn_delegate`）是这样工作的：

- 新建一个子 `Pico`：只读、`approval_policy="never"`、`max_steps` 默认 3、`depth+1`；深度到上限后不再提供 `delegate` 工具。
- 父 agent 的历史经过 `clip(history_text, 300)` 截成 300 字符后，塞进子 agent 的 `memory.notes` 当上下文。
- 子 agent 的 `final` 文本原样返回给父 agent：`"delegate_result:\n" + child.ask(task)`。

| 问题 | 位置 / 原因 | 影响 |
|---|---|---|
| 一次只能派一个，只能串行 | `delegate` 是单次调用 | 没有并行带来的提速 |
| 只读 | 子 agent 固定 `read_only=True` | 不能分工写代码 |
| 上下文交接很粗 | 父历史被截成 300 字符 | 子 agent 拿不到关键事实，只能自己重新读文件 |
| 返回的是自由文本 | `final` 原样返回 | 父 agent 没法结构化地使用；模型自己说"改了哪些文件"并不可信 |
| **共用同一个 model_client 实例** | 父子用同一个对象；`last_completion_metadata` 挂在实例上；`FakeModelClient.outputs` 也是共享列表 | 一旦并行，usage 统计会串到别人身上，测试剧本的顺序也会乱 |
| **只读子 agent 能写持久记忆** | 子 agent 结束时也会调用 `promote_durable_memory`；只要子任务文字里有 remember、记录之类的词，就会写入 `.pico/memory/` | 绕过了 `read_only` |
| 子 session 和父 session 放在同一目录 | `SessionStore.latest()` 按文件修改时间取最新的 | 委派中途崩溃后，`--resume latest` 可能恢复到子会话 |
| 子 run 和父 run 没有关联 | 没有 `parent_run_id` 字段 | 复盘时找不到一次协作对应的整棵运行树 |
| 没有全局预算 | 只有子 agent 自己的 `max_steps` | 多个 agent 一起跑时，总成本不受控 |
| 审批靠 `input()` | 在 `approve()` 里 | 多个线程同时请求审批时，终端提示会混在一起 |
| 已知的前置 bug | 批处理结果重复写进历史；memory 写两次且没加锁 | 并发会放大这两个问题 |

> 💬 **白话解释**
> 现在的 `delegate` 相当于"派一个只能看、不能动手的帮手去查点东西，回来口头汇报"。它的毛病是：
> - **只能派一个，还只能看不能改**，所以谈不上分工；
> - **交代任务太草率**：只把之前聊过的内容截一小段（300 个字）给帮手，帮手很多事不知道，只好自己重新翻文件；
> - **汇报是随口说的**：帮手说"我改了 A 文件"，系统没法核实；
> - **几个隐藏问题**：大家共用同一个"模型电话"，同时打就串线；号称只读的帮手其实能往长期记忆里写东西；帮手的会话记录和组长的混在一个文件夹，程序崩溃后可能接错记录；没有总预算，人一多钱就花不住；审批时都往同一个终端里问，多人同时问就乱成一团。
> 这一节是在找"要改什么"，后面每个设计点都对应这里的某个问题。

> 📖 **本节术语**
> - **delegate（委派）**：pico 现有的一个工具，作用是派出一个子 agent。
> - **read_only（只读）**：只能读文件，不能写文件、不能执行有风险的命令。
> - **approval_policy（审批策略）**：执行高风险操作前要不要问人。`ask` 问人，`auto` 自动同意，`never` 一律拒绝。
> - **max_steps（最大步数）**：一个 agent 一次任务最多能调用多少次工具。
> - **depth（深度）**：委派到第几层。组长是 0，组员是 1。
> - **history（历史）**：agent 和用户、工具之间的对话记录。
> - **clip（截断）**：超过长度上限的部分直接剪掉。
> - **memory.notes**：pico 工作记忆里的"笔记"区。
> - **final**：模型给出的最终答案（`<final>...</final>`）。
> - **实例 / 实例属性**：用同一个"模具"（类）造出来的一个具体对象叫实例；挂在这个对象身上的数据叫实例属性。两个人用同一个实例，就是在改同一份数据。
> - **model_client**：负责调用模型 API 的对象，相当于"模型电话"。
> - **usage**：一次模型调用用了多少 token 之类的统计。
> - **FakeModelClient**：测试用的假模型，按预先写好的"剧本"依次返回答案。
> - **持久记忆（durable memory）**：跨会话长期保存的记忆，存在 `.pico/memory/`。
> - **session（会话）**：一次连续的对话和它的状态，存在 `.pico/sessions/`，可以恢复。
> - **run（运行）**：一次 `ask()` 的完整执行过程，工件存在 `.pico/runs/<run_id>/`。
> - **`--resume latest`**：启动时恢复最近一次会话的命令行参数。
> - **运行树**：父 run 和它派出的所有子 run 组成的树状关系。
> - **全局预算**：整次协作所有 agent 加起来能用的步数、token 上限。
> - **线程**：同一个程序里可以同时跑的执行流，多个线程共享同一份内存。
> - **加锁**：让多个线程排队修改同一份数据，避免同时改造成错乱。

---

## 2. 参考的设计思路

这些产品更新很快，下面只取比较稳定的公开思路，细节以各自官方文档为准。

| 来源 | 思路 | pico 借鉴什么 |
|---|---|---|
| Claude Code 子 agent | 子 agent 有独立的上下文窗口；按角色定义（系统提示 + 工具白名单）；只把总结交回主线程；可以并行 | 角色定义文件、上下文隔离、只回传摘要 |
| Codex | 任务在隔离环境里执行；可以并行跑多次尝试再挑一个 | 隔离执行、best-of-N |
| Aider 的 architect / editor 模式 | 一个模型负责出方案，另一个模型负责落地改代码 | 规划和执行分开；不同角色可以用不同模型 |
| 业界常见做法 | 用 git worktree 隔离并行改动，最后再合并 | Worker 的隔离和合并方式 |

> 💬 **白话解释**
> 我们不是从零发明，而是看主流 coding agent 怎么做多人协作，各取一招：
> - 学 Claude Code：每个组员有**自己独立的记事本**，不会被别人的内容塞满；按"岗位说明书"定义组员能干什么；组员干完**只交一份总结**，不把过程全倒给组长。
> - 学 Codex：每个人在**自己的隔离空间**里干活；难题可以让好几个人**各做一版，挑最好的**。
> - 学 Aider：**想方案的和动手改代码的分开**，各做各擅长的。
> - 学业界通用做法：用 git worktree 给每个改代码的组员单独开一个工作文件夹（第 8 节详细讲）。

> 📖 **本节术语**
> - **Claude Code / Codex / Aider**：市面上流行的命令行 coding agent，分别出自 Anthropic、OpenAI 和开源社区。
> - **上下文窗口**：模型一次最多能"看到"的文字量，单位是 token。
> - **系统提示（system prompt）**：在对话开头告诉模型"你是谁、规则是什么"的那段文字。
> - **工具白名单**：明确列出这个 agent 能用哪些工具，没列出的一律不能用。
> - **主线程**：这里指组长那条主对话。
> - **best-of-N**：同一个任务让 N 个 agent 各做一次，选出最好的那份结果。
> - **git worktree**：见第 8 节和附录 A。

---

## 3. 总体架构

```
Orchestrator（主 Pico，depth=0，负责拆任务、决策、集成，不直接大量写代码）
 ├─ TaskBoard        .pico/runs/<run>/board.json：任务、依赖、状态、结果（原子写）
 ├─ Scheduler        ThreadPoolExecutor，默认 max_concurrency=3
 │    ├─ Explorer ×N 只读；共享主工作区
 │    ├─ Worker   ×N 可写；每个有独立的 git worktree 和声明的写入范围
 │    └─ Reviewer    只读 + run_shell；在集成 worktree 里跑测试
 ├─ Integrator       把各 worker 的分支合并到集成 worktree；验证通过后再应用回主工作区
 ├─ ApprovalBroker   把所有子 agent 的审批请求排队，一个一个处理
 └─ BudgetLedger     全局的步数、token、任务数、重试次数上限（线程安全）
```

**设计原则**：

1. **星型拓扑**：子 agent 之间不直接通信，只通过 TaskBoard 和 Orchestrator 交换信息。这样过程可审计，也不会互相等待导致死锁。
2. **最多一层委派**：子 agent 拿不到编排类工具，深度固定不超过 1。
3. **并行写必须隔离**：每个 Worker 有独立的 worktree；同一文件在同一时间只能有一个负责的 Worker。
4. **交接结构化**：父 → 子传 Brief，子 → 父回 Result，都是固定结构的 JSON。
5. **父 agent 只收摘要**：子 agent 的完整过程保存在它自己的 run 目录，父历史里每个任务只记一条。
6. **出问题就停**：遇到合并冲突、测试失败、越界写入、超预算，一律交回 Orchestrator 决定，不自动强行合并。

> 💬 **白话解释**
> 把它想成一个小开发团队：
>
> | 组件 | 相当于 | 干什么 |
> |---|---|---|
> | Orchestrator | 组长 | 拆任务、派活、看结果、拍板，自己尽量不下场写代码 |
> | TaskBoard | 墙上的任务看板 | 写着有哪些任务、谁在做、做到哪、结果如何；存成文件，断电重启也不丢 |
> | Scheduler | 排班员 | 看哪些任务前置条件都满足了，就安排人去做，最多同时 3 个 |
> | Explorer | 调研员 | 只看不改，负责查清楚代码现状 |
> | Worker | 开发 | 真正改代码，每人在自己的文件夹里改，互不干扰 |
> | Reviewer | 测试 / 验收 | 把大家的改动合在一起后跑测试，看有没有问题 |
> | Integrator | 合稿编辑 | 把各个开发的改动合到一起，检查有没有打架、有没有改了不该改的 |
> | ApprovalBroker | 审批窗口 | 所有"要不要允许执行这个命令"的请求排队，一个一个问你 |
> | BudgetLedger | 财务 | 记录整个团队用了多少步、多少 token，超支就叫停 |
>
> 六条原则的意思：
> 1. 组员之间不私聊，所有沟通都经过看板和组长，这样每件事都有记录，也不会出现"你等我、我等你"的僵局；
> 2. 组员不能再往下招人；
> 3. 同时改代码的人，各改各的文件夹、各管各的文件；
> 4. 交代任务和汇报结果都用固定格式的表格，不随口说；
> 5. 组长只看组员交上来的总结，详细过程归档备查；
> 6. 一出问题就停下来让组长决定，不硬着头皮往下做。

> 📖 **本节术语**
> - **主工作区**：你平时打开、编辑的那个项目文件夹，这里是 `E:\Github_program\pico`。
> - **原子写**：先写到临时文件，写完整后再一次性替换原文件，保证文件要么是旧的完整版、要么是新的完整版，不会出现写了一半的情况。
> - **ThreadPoolExecutor（线程池）**：Python 自带的工具，预先准备几个线程，把任务丢进去自动分配执行。
> - **max_concurrency（最大并发数）**：同一时刻最多能有几个任务在跑。
> - **run_shell**：pico 执行命令行命令的工具。
> - **集成 worktree**：专门用来把所有人改动合并在一起的那个工作文件夹，见第 8 节。
> - **线程安全**：多个线程同时用也不会出错。
> - **星型拓扑**：所有成员都只和中心（组长）相连，成员之间不相连，画出来像星星。相对的是"网状"，人人都能互相连。
> - **审计**：事后检查、追查记录。
> - **死锁**：A 等 B、B 又在等 A，结果谁都动不了。
> - **编排类工具**：`plan_tasks`、`run_tasks` 这类用来派活的工具，只给组长用。
> - **JSON**：一种用 `{}`、`[]`、`"键": 值` 表示结构化数据的文本格式。
> - **合并冲突**：两个人改了同一个文件的同一处，git 不知道该保留谁的。
> - **越界写入**：改了任务声明范围之外的文件，见第 5.4 节。

---

## 4. 角色定义：`.pico/agents/*.md`

做法类似 `AGENTS.md`：用户可以自己新增或覆盖角色。内置的四个角色放在 `pico/agents/builtin/`。

```markdown
---
name: explorer
description: 只读调查代码，定位相关文件、调用关系和问题原因
tools: [list_files, read_file, search]
max_steps: 8
write_scope: none        # none | declared（由任务声明） | worktree
budget_ratio: 0.25       # 占父 agent prompt token 预算的比例
model: inherit           # inherit | 指定的 provider:model
---
你是 explorer。只做调查，不修改任何文件。
所有结论都必须附上证据（path:line）……
```

| 角色 | 工具 | 写入能力 | 典型步数 |
|---|---|---|---|
| explorer | list_files / read_file / search | 无 | 8 |
| worker | 全部基础工具（不含编排类工具） | 只能写自己的 worktree，且限于声明的 write_scope | 12 |
| reviewer | read_file / search / run_shell | 无（run_shell 只用来跑测试） | 6 |
| planner（可选） | 只读工具 | 无 | 4 |

各字段对应到现有 `Pico` 的参数：`tools` → `allowed_tools`（已实现）；`write_scope: none` → `read_only=True`；`budget_ratio` → 按比例缩小后的 `context_profile`；正文追加到 prefix 里。frontmatter 自己解析，只支持 `key: value` 和 `[a, b]` 两种写法，保持零依赖。

> 💬 **白话解释**
> 这一节定的是"岗位说明书"。每个岗位用一个 Markdown 文件描述：叫什么、负责什么、能用哪些工具、最多干几步、能不能改文件、能用多少预算、用哪个模型；文件下半部分是给这个岗位的工作守则。
> 默认有四个岗位：调研员（explorer）、开发（worker）、验收（reviewer），以及可选的方案设计（planner）。你也可以在自己项目的 `.pico/agents/` 里加新岗位，或者改掉默认岗位的设定。
> 这些设定大多能直接对应到 pico 已有的开关上（比如工具白名单 pico 已经支持），所以实现起来不用大改。

> 📖 **本节术语**
> - **Markdown（.md）**：一种用 `#`、`-` 等符号表示格式的纯文本写法。
> - **AGENTS.md**：业界约定俗成的一个文件，用来给 coding agent 写项目级说明。
> - **frontmatter**：Markdown 文件开头两行 `---` 之间的那段配置，写成 `键: 值`。
> - **内置角色**：pico 自带的默认岗位。
> - **budget_ratio（预算比例）**：子 agent 能用的 prompt 预算占组长预算的几成，0.25 就是四分之一。
> - **prompt**：每次发给模型的完整输入文字。
> - **token**：模型计量文字的单位，大约 1 个汉字或 3～4 个英文字符算 1 个。
> - **inherit（继承）**：沿用组长正在用的那个模型。
> - **provider:model**：写法如 `deepseek:deepseek-v4-pro`，冒号前是模型服务商，冒号后是模型名。
> - **allowed_tools**：pico 已有的参数，限制 agent 能用的工具。
> - **context_profile**：上一轮改造加的上下文预算配置。
> - **prefix**：每次 prompt 最前面那段固定内容（规则、工具列表、项目概况）。
> - **零依赖**：不需要安装任何第三方 Python 包。

---

## 5. TaskBoard 数据模型

文件位置：`.pico/runs/<run_id>/board.json`。它记录一次多 agent 协作的全部任务：每个任务要做什么、能动哪些文件、依赖谁、现在到哪一步、产出了什么、花了多少。

### 5.1 完整示例

```json
{
  "schema_version": 1,
  "run_id": "run_20261008-170000-a1b2c3",
  "base_commit": "abc1234",
  "created_at": "2026-10-08T09:00:00+00:00",
  "tasks": [
    {
      "task_id": "t2",
      "role": "worker",
      "goal": "给 ContextManager 增加 token 预算校验",
      "acceptance": ["tests/test_context_manager.py 通过", "compact 模式输出不变"],
      "verify": ["python -m pytest tests/test_context_manager.py -q"],
      "inputs": {
        "files": ["pico/context_manager.py"],
        "facts": ["预算以字符渲染，token 作为第二道上限"]
      },
      "depends_on": ["t1"],
      "write_scope": ["pico/context_manager.py", "pico/context_budget.py"],
      "model": "inherit",
      "variant_of": "",

      "status": "failed",
      "stop_reason": "verify_failed",
      "error": "tests/test_context_manager.py::test_xxx AssertionError ...",
      "attempt": 1,
      "max_attempts": 2,
      "child_run_id": "run_20261008-170105-d4e5f6",
      "attempts_history": [
        {
          "attempt": 1,
          "child_run_id": "run_20261008-170105-d4e5f6",
          "model_resolved": "deepseek:deepseek-v4-pro",
          "status": "failed",
          "stop_reason": "verify_failed",
          "error": "tests/test_context_manager.py::test_xxx AssertionError ...",
          "started_at": "2026-10-08T09:01:05+00:00",
          "finished_at": "2026-10-08T09:03:40+00:00",
          "usage": {"steps": 9, "prompt_tokens_est": 84000, "duration_ms": 155000}
        }
      ],
      "model_resolved": "deepseek:deepseek-v4-pro",
      "worktree": ".pico/worktrees/t2",
      "branch": "pico/run_20261008-170000-a1b2c3/t2",
      "created_at": "2026-10-08T09:00:00+00:00",
      "started_at": "2026-10-08T09:01:05+00:00",
      "finished_at": "2026-10-08T09:03:40+00:00",
      "result": {"status": "done", "summary": "...", "changed_files": ["pico/context_manager.py"]},
      "usage": {"steps": 9, "prompt_tokens_est": 84000, "duration_ms": 155000}
    }
  ]
}
```

字段分两类，这个区分很重要：

- **规划字段**：Orchestrator（模型）调用 `plan_tasks` 时填写，runtime 会校验。
- **系统字段**：只能由 runtime 写入。模型在 `plan_tasks` 里填了也会被忽略，防止模型伪造状态或结果。

> 💬 **白话解释**
> TaskBoard 就是墙上那块任务看板，只不过存成了一个 JSON 文件。看板上每张"任务卡"写着：这活是啥、交给哪个岗位、怎样算做完、能动哪些文件、要等谁先做完、现在什么状态、做了几次、结果怎样、花了多少时间和 token。
> 卡片上的内容分两种：一种是**组长写的计划**（做什么、怎么验收）；另一种是**系统自动填的记录**（状态、结果、耗时）。第二种组长没法改，防止"自己给自己打分"。
> 示例里这张卡的意思是：t2 是个开发任务，第一次做完后跑测试没过（`verify_failed`），还有一次重试机会，第一次的记录已经存进了 `attempts_history`。

> 📖 **本节术语**
> - **schema**：数据的结构约定，规定有哪些字段、什么类型。
> - **runtime**：pico 程序本身的执行逻辑（不是模型）。
> - **plan_tasks**：组长用来写入任务计划的工具，见第 6 节。
> - **伪造状态**：模型谎报"我做完了"之类的情况。

### 5.2 顶层字段

| 字段 | 含义 | 由谁写 | 说明 |
|---|---|---|---|
| `schema_version` | board.json 的格式版本号 | 系统 | 恢复时先检查。版本对不上就拒绝恢复，和现有 checkpoint 的 `schema-mismatch` 处理方式一样 |
| `run_id` | 这次协作所属的父 run，也就是组长这次 `ask()` 的 run | 系统 | 把 board 和组长的 trace、report 关联起来；子 run 用 `parent_run_id` 指回这里，形成运行树 |
| `base_commit` | 规划时主工作区当前的 commit（`git rev-parse HEAD`） | 系统 | 所有 worker 的 worktree 都从这个 commit 开始，保证起点相同，合并时比较的基准一致。主工作区有未提交的改动时，会一起带进 worktree，并在 trace 里记一条警告 |
| `created_at` | board 创建时间 | 系统 | UTC，ISO 8601 格式 |
| `tasks` | 任务列表 | 两类都有 | 见下 |

> 💬 **白话解释**
> 这几项是"看板本身"的信息：格式版本（防止新旧程序读错）、属于哪次协作、大家从代码的哪个版本开始改、看板是什么时候建的。
> `base_commit` 最关键：好比所有人拿到的都是"10 月 8 日上午 9 点那一版"的代码复印件，从同一个起点开始改，最后才好比较、好合并。

> 📖 **本节术语**
> - **trace**：pico 每次 run 记录的事件流水账（`trace.jsonl`）。
> - **report**：一次 run 结束后的总结报告（`report.json`）。
> - **checkpoint**：pico 已有的"进度存档"机制。
> - **commit（提交）**：git 里的一个存档点，记录某一时刻所有文件的完整状态，见第 8 节。
> - **HEAD**：git 里"当前所在的那个 commit"。
> - **`git rev-parse HEAD`**：查看当前 commit 编号的命令。
> - **未提交的改动**：改了文件，但还没存成 commit。
> - **UTC**：协调世界时，香港时间 = UTC + 8 小时。
> - **ISO 8601**：国际标准时间写法，如 `2026-10-08T09:00:00+00:00`，末尾 `+00:00` 表示 UTC。

### 5.3 任务字段

#### ① 身份和分工

| 字段 | 含义 | 由谁写 | 校验和说明 |
|---|---|---|---|
| `task_id` | 任务在本 board 内的唯一编号，如 `t2` | 规划 | 必须唯一；只允许 `[a-z0-9_-]`，因为分支名、文件夹名都要用它 |
| `role` | 由哪个岗位执行：`explorer` / `worker` / `reviewer` / `planner` | 规划 | 必须是已注册的角色。角色决定工具白名单、步数、预算、能否写文件，**任务本身不能突破角色的权限** |
| `model` | 想用哪个模型 | 规划 | 默认 `inherit`。不能超出 CLI 已配置的 provider |
| `variant_of` | 如果这是 best-of-N 的候选之一，填原任务的 ID；否则为空 | 规划 | 同一组候选的 write_scope 允许重叠（它们本来就是竞争关系，只会选一个合并） |
| `model_resolved` | 实际用的模型（`provider:model`） | 系统 | 开始执行时把 `model` 解析成具体模型后记下，方便评测时分清是模型的问题还是 harness 的问题 |

> 💬 **白话解释**
> 这几项回答"这张卡是谁、归哪个岗位、用哪个大脑"。`variant_of` 用在"同一道难题让几个人各做一版"的时候：几张卡都指向同一个原任务，说明它们是一组竞争方案，最后只挑一份。`model_resolved` 是实际用的模型，因为 `inherit` 只说"跟组长一样"，具体是哪个要记清楚。

> 📖 **本节术语**
> - **`[a-z0-9_-]`**：只能用小写字母、数字、下划线和短横线。
> - **已注册的角色**：在 `pico/agents/builtin/` 或 `.pico/agents/` 里有定义文件的角色。
> - **harness**：包在模型外面的那层控制程序，也就是 pico 本身。

#### ② 任务内容（会生成 Brief 交给子 agent）

| 字段 | 含义 | 由谁写 | 校验和说明 |
|---|---|---|---|
| `goal` | 一句话说明要做成什么 | 规划 | 不能为空，有长度上限。它会成为子 agent 的"当前用户请求"，在子 agent 那边永远不会被裁剪 |
| `acceptance` | 验收标准列表（文字描述） | 规划 | 写成可检查的条件，比如"`tests/test_x.py` 通过"。子 agent 用它判断什么时候算完，reviewer 按它逐条检查 |
| `verify` | 可选的自动验收命令列表 | 规划 | 子 agent 结束后，runtime 在它的工作目录里依次执行，**全部退出码为 0 才算 `done`**。命令不经过 shell 直接执行（复用 evaluator 里的 `_run_verifier` 拆参数方式）；命令是模型写的，所以同样要经过 ApprovalBroker 审批 |
| `inputs.files` | 建议子 agent 优先看的文件 | 规划 | **只是提示，不是权限**，子 agent 仍可读其他文件。生成 Brief 时会附上这些文件在组长 memory 里的摘要，并用 sha256 检查是否还新鲜，过期的不传 |
| `inputs.facts` | 组长已确认的事实 | 规划 | 原样写进 Brief，有总长度上限。替代现在"把父历史截 300 字符"的做法 |

> 💬 **白话解释**
> 这几项就是"交代任务"的内容：要做成什么（goal）、怎样算合格（acceptance）、用什么命令自动检查合格（verify）、建议先看哪些文件（inputs.files）、有哪些已经确认过、不用再查的事实（inputs.facts）。
> `acceptance` 和 `verify` 的区别：`acceptance` 是写给人和模型看的文字标准；`verify` 是机器能直接跑的检查命令，跑通了才算数，不靠模型自己说"我做完了"。

> 📖 **本节术语**
> - **Brief（任务简报）**：系统根据这些字段整理好、交给子 agent 的任务说明，见第 7 节。
> - **裁剪**：prompt 超出预算时把部分内容截短或去掉。
> - **退出码**：命令执行结束时返回的数字，0 表示成功，非 0 表示失败。
> - **shell**：命令行解释器，Windows 是 cmd / PowerShell，Linux 是 bash。不经过 shell 直接执行更安全，也不会有 Windows 下的引号问题。
> - **sha256**：一种"文件指纹"算法。文件只要改一个字，指纹就完全不同，用来判断文件变没变。
> - **新鲜**：文件摘要生成之后，文件没有再被改过。

#### ③ 调度约束

| 字段 | 含义 | 由谁写 | 校验和说明 |
|---|---|---|---|
| `depends_on` | 必须先完成的任务 ID 列表 | 规划 | 引用的 ID 必须存在，整体不能有环（做拓扑排序检查）。所有依赖都 `done` 时，任务才进入"就绪"状态；只要有依赖失败，本任务就变成 `blocked`。依赖任务的 `result.summary` 会自动带进本任务的 Brief |
| `write_scope` | 允许改动的文件或目录列表（相对仓库根目录） | 规划 | 规则见 5.4 |

> 💬 **白话解释**
> `depends_on` 是"先后顺序"：比如"写测试"要等"查清楚接口"做完才能开始。系统会检查有没有"A 等 B、B 又等 A"这种绕圈的情况。前面的任务做完后，它的总结会自动交给后面的任务。
> `write_scope` 是"这张卡允许动哪些文件"的清单，详见下一小节。

> 📖 **本节术语**
> - **环**：依赖关系绕成一个圈，例如 t1 依赖 t2、t2 又依赖 t1，这样永远开始不了。
> - **拓扑排序**：把有先后依赖的任务排成一个合法顺序的算法，排不出来就说明有环。
> - **就绪**：前置任务都完成了，可以开始做。

### 5.4 `write_scope` 和 `scope_violation`（越界写入）

**write_scope 是什么**：任务开始**之前**，组长声明"这个任务只会改这些文件"。

写法：
- 精确文件：`pico/context_manager.py`
- 整个目录：以 `/` 结尾，如 `pico/agents/`，表示这个目录下的所有文件，包括新建的文件
- explorer、reviewer 必须为空（它们不能写）
- 路径会先用现有的 `canonicalize_path` 规范化，防止用 `../`、`./pico//x.py` 这类写法绕过

**规划时的检查**：任意两个**可能同时运行**的 worker（彼此之间没有直接或间接的依赖关系，并且不是同一组 `variant_of` 候选），write_scope 不能有交集，否则 `plan_tasks` 直接拒绝。

**完成后的检查**：子 agent 结束后，runtime 在它的 worktree 里查出**实际改动了哪些文件**：
- `git diff --name-only <base_commit>`：修改或删除的已跟踪文件
- `git ls-files --others --exclude-standard`：新建的文件
- 忽略 `__pycache__`、`.pytest_cache` 等自动生成的目录（复用 `IGNORED_PATH_NAMES`）

只要有一个实际改动的文件不在 write_scope 里，任务状态就是 **`scope_violation`（越界写入）**，`stop_reason="scope_violation"`，`error` 里列出越界的文件。

**越界之后怎么办**：默认**不合并**，worktree 保留。交回组长三选一：
1. **放行**：用 `plan_tasks` 把 write_scope 扩大到包含这些文件，系统会重新检查和其他并行任务有没有重叠，没有冲突才允许合并；
2. **重试**：在新的 Brief 里写明"上次改了 X，不允许改"，重做一遍；
3. **丢弃**：不要这次的结果。

> 💬 **白话解释**
> 打个比方：三个人一起改一份合同，组长事先分好——小王只改第 1 章，小李只改第 2 章，小张只改附录。这份"分工清单"就是 `write_scope`。
> 因为提前分好了、互不重叠，三人才能**同时**动手，不用担心改到一块去。
> 交稿时，系统不听小王说"我只改了第 1 章"，而是**逐页对比原稿**，看他到底改了哪些页。要是发现他顺手改了第 2 章的一段，这就叫 **`scope_violation`（越界写入）**。
> 为什么要严查越界？
> - 第 2 章是小李负责的，小王也改了，合稿时两人的改动就会打架；
> - 越界经常是"走捷径"的信号，比如模型为了让测试通过，偷偷把测试文件改了；
> - 并行安全的前提是"各管各的"，越界就破坏了这个前提。
> 发现越界后不会直接扔掉，而是把稿子先放一边，让组长决定：是认可这次改动（把范围扩大），还是让他重做，或者干脆不要了。

> 📖 **本节术语**
> - **交集**：两个清单里都有的部分。
> - **canonicalize_path（路径规范化）**：把各种写法的路径统一成标准形式，比如 `./pico/../pico/x.py` 统一成 `pico/x.py`。
> - **已跟踪文件**：已经被 git 管理（之前提交过）的文件。
> - **`git diff --name-only`**：列出相对某个 commit 改动过的文件名。
> - **`git ls-files --others --exclude-standard`**：列出新建的、还没被 git 管理的文件（排除 `.gitignore` 里忽略的）。
> - **IGNORED_PATH_NAMES**：pico 已有的忽略目录列表。

### 5.5 运行状态字段（系统字段）

| 字段 | 含义 | 说明 |
|---|---|---|
| `status` | 任务当前状态 | 见 5.7 状态转换 |
| `stop_reason` | 最近一次结束的原因代码 | 例如 `final_answer_returned`（正常完成）、`verify_failed`、`scope_violation`、`budget_exhausted`、`step_limit_reached`、`result_parse_failed`、`dependency_failed`、`cancelled`、`variant_not_selected`、`model_error`、`tool_timeout`。尽量复用 `task_state.py` 已有的常量 |
| `error` | 人能看懂的错误详情 | 截断到 500 字符，如失败测试名、越界文件列表、冲突文件列表 |
| `attempt` | 当前是第几次尝试 | 从 1 开始，失败重试时加 1 |
| `max_attempts` | 最多尝试几次 | 默认 2；可在规划时指定，但不能超过 BudgetLedger 的全局重试上限 |
| `child_run_id` | **当前这次**尝试的子 agent run ID | 指向 `.pico/runs/<child_run_id>/` |
| `attempts_history` | 每次尝试的记录列表 | 每次尝试结束时追加一条：`attempt`、`child_run_id`、`model_resolved`、`status`、`stop_reason`、`error`、`started_at`、`finished_at`、`usage`。重试时 `child_run_id` 会换成新的，旧记录靠这里保留 |
| `worktree` | worker 的独立工作文件夹，如 `.pico/worktrees/t2` | 只有 worker 有，见第 8 节 |
| `branch` | worker 的分支名，如 `pico/<run_id>/t2` | 只有 worker 有，见第 8 节 |

> 💬 **白话解释**
> 这些是系统自动填的"进度记录"：
> - `status`：现在处于哪一步（排队、在做、做完、失败……）；
> - `stop_reason` + `error`：上一次为什么停下来，一个是代码方便程序判断，一个是文字方便人看。不用再去翻详细日志；
> - `attempt` / `max_attempts`：第几次做、最多能做几次，防止无限重试烧钱；
> - `child_run_id`：这次是哪个"组员实例"在做，用它能找到完整的工作日志；
> - `attempts_history`：每一次尝试的"档案袋"，重做之后上一次的记录也不会丢；
> - `worktree` / `branch`：开发人员的独立工作文件夹和他的修改线（第 8 节有详细解释和图）。

> 📖 **本节术语**
> - **常量**：程序里写死、不会变的值，例如 `STOP_REASON_STEP_LIMIT_REACHED`。
> - **截断到 500 字符**：超过 500 个字符的部分剪掉，免得看板文件太大。

### 5.6 时间、产出和消耗（系统字段）

| 字段 | 含义 | 说明 |
|---|---|---|
| `created_at` | 任务卡创建时间 | `plan_tasks` 写入时 |
| `started_at` | 当前尝试的开始时间 | 进入 `running` 时 |
| `finished_at` | 当前尝试的结束时间 | 离开 `running` 时 |
| `result` | 子 agent 交回的结构化结果（第 7.2 节的 Result），**并经过 runtime 核对** | 没结束时是 `null`。`changed_files` 用 worktree 的真实改动覆盖；`tests` 用子 agent trace 里 `run_shell` 的真实退出码核对；文本字段做长度截断 |
| `usage.steps` | 实际用了多少工具步 | 计入全局预算 |
| `usage.prompt_tokens_est` | 所有模型调用的估算 prompt token 总和 | 用 `estimate_tokens()` 估算，是成本指标 |
| `usage.duration_ms` | 当前尝试的墙钟耗时（毫秒） | 用来算加速比 |

注意 **`result.status` 和任务的 `status` 是两回事**：前者是子 agent 自己说的（done / blocked / failed），后者是 runtime 的最终判定。子 agent 说自己 `done`，但 verify 没过，任务 `status` 就是 `failed`；改了范围外的文件，就是 `scope_violation`。

> 💬 **白话解释**
> - 三个时间戳让我们能画出"谁在几点到几点干活"的时间线，一眼看出有没有真的同时开工；
> - `result` 是组员交上来的报告，但系统会**核实**：报告说改了哪些文件，以实际对比为准；报告说测试过了，以实际运行记录为准；
> - `usage` 是这张卡的"花费账单"：用了几步、大约多少 token、花了多长时间。
> 最后那句话的意思是：**组员自己说"做完了"不算数，系统核实通过才算数**。

> 📖 **本节术语**
> - **墙钟耗时**：现实中实际过去了多少时间（和"CPU 时间"相对）。
> - **毫秒（ms）**：千分之一秒。
> - **加速比**：所有任务各自耗时加起来 ÷ 实际总耗时。比如 3 个任务各要 10 分钟，同时做总共 12 分钟就做完了，加速比 = 30 ÷ 12 = 2.5。
> - **estimate_tokens()**：上一轮改造加的 token 估算函数。

### 5.7 状态转换

```
              plan_tasks
                  │
                  ▼
   ┌──────── pending ◄─────────────────────────────────┐
   │              │ 依赖全部 done，且预算申请成功          │ 失败且 attempt < max_attempts
   │              ▼                                     │ （attempt+1，新 Brief 里带上失败原因）
   │          running ──────────────► failed ───────────┘
   │              │                     │ 已达 max_attempts → 保持 failed，交回 Orchestrator
   │              ├──► done            （verify 通过、未越界）
   │              ├──► scope_violation （实际改动超出 write_scope）
   │              └──► blocked         （子 agent 自报卡住，比如缺信息）
   │
   ├──► blocked   （依赖任务 failed / cancelled / scope_violation，stop_reason=dependency_failed）
   └──► cancelled （Orchestrator 取消、预算耗尽、严重错误、best-of-N 落选；running 中也可取消）
```

| 状态 | 含义 | 是否终态 |
|---|---|---|
| `pending` | 排队中，等依赖完成或等空位 | 否 |
| `running` | 子 agent 正在执行 | 否 |
| `done` | 完成，并且已核实 | 是 |
| `failed` | 失败（模型出错、步数用完、verify 没过……） | 达到重试上限后是 |
| `blocked` | 卡住，需要组长补充信息或重新规划 | 否（等组长处理） |
| `scope_violation` | 越界写入，结果暂不合并 | 等组长决定 |
| `cancelled` | 被取消 | 是 |

- **崩溃后恢复**：所有 `running` 改回 `pending`；`attempt` 不加 1（因为不是任务本身失败）；已有的 worktree 保留；`done` 的任务不重新执行。
- **写看板只有一个人**：只有 Scheduler 所在的主线程写 board。子线程把结果放进队列，由主线程统一写入，每次都原子写，所以不会出现写了一半的 JSON。

> 💬 **白话解释**
> 一张任务卡从创建到结束会经过几种状态，就像快递单的"已下单 → 运输中 → 已签收"：
> - 刚建好是 **pending（排队）**；
> - 轮到它且前置任务都完成了，就变成 **running（在做）**；
> - 做完后系统核实：一切正常就是 **done**；测试没过等情况是 **failed**，还有重试次数就回去排队重做，没有了就交给组长；改了不该改的文件是 **scope_violation**；组员说"缺信息做不下去"是 **blocked**；
> - 被组长叫停，或者钱花完了，就是 **cancelled**。
> "终态"是指不会再变的最终状态。
> 如果程序中途崩溃：重启后把"在做"的卡片改回"排队"重新做（不算失败次数），已经做完的不会重做。
> 为了防止多人同时改看板改乱了，规定只有一个人（主线程）能写看板，其他人把结果交给它代写。

> 📖 **本节术语**
> - **终态**：到这里就结束了、不会再变成别的状态。
> - **队列**：先进先出的排队结构，大家把东西放进去，由一个人按顺序取出来处理。
> - **主线程**：程序启动时的那个线程，这里负责调度和写看板。

### 5.8 一张任务卡的完整经历（t2）

| 时间（UTC） | 发生了什么 | 字段变化 |
|---|---|---|
| 09:00:00 | 组长 `plan_tasks` | `status=pending`，`attempt=1`，`result=null`，`created_at` 写入；系统生成 `worktree`、`branch` |
| 09:01:05 | t1 完成，t2 开始 | `status=running`，`started_at` 写入，创建 worktree，写入 `child_run_id`、`model_resolved` |
| 09:03:40 | 子 agent 结束，verify 失败 | `status=failed`，`stop_reason=verify_failed`，`error` 写入失败测试名，`finished_at` 写入；追加 `attempts_history[0]` |
| 09:03:41 | 还能重试（1 < 2） | `status=pending`，`attempt=2`，`error` 会写进新 Brief |
| 09:03:42 | 第 2 次开始 | `status=running`，`child_run_id` 换成新的 run，`started_at` 更新 |
| 09:05:10 | verify 通过、没越界 | `status=done`，`stop_reason=final_answer_returned`，`result` 写入（已核实），追加 `attempts_history[1]` |

---

## 6. Orchestrator 新增的工具

调用格式仍然是现在的 `<tool>{...}</tool>` 文本协议。

| 工具 | 作用 | runtime 侧的校验 |
|---|---|---|
| `plan_tasks(tasks)` | 写入或更新 TaskBoard | 角色存在；依赖无环；**能并行的 worker 之间 write_scope 不重叠**；任务总数不超过上限 |
| `run_tasks(task_ids \| "ready")` | 执行所有就绪的任务，**等这一批全部结束后**返回每个任务的 Result 摘要 | 先向 BudgetLedger 申请预算；并发数受上限约束 |
| `board()` | 查看压缩后的任务板 | — |
| `integrate(task_ids)` | 把指定 worker 的分支合并到集成 worktree，返回改动统计或冲突列表 | 合并前先做越界检查 |
| `apply_integration()` | 把集成结果应用回主工作区 | 高风险操作，必须审批；要求 reviewer 已通过 |

**为什么 `run_tasks` 设计成阻塞调用**：pico 是同步循环，模型每轮只输出一个动作。把"派发"和"等结果"合成一次调用最简单，模型也不用反复轮询状态。

**兼容现有接口**：`delegate(task)` 保留，内部改成"建一个 explorer 任务并执行"，现有测试不受影响。

> 💬 **白话解释**
> 这是组长手里的五个"按钮"：
> - `plan_tasks`：在看板上贴任务卡（系统会检查有没有绕圈依赖、同时开工的人有没有分到同一个文件）；
> - `run_tasks`：说"开工"，然后**一直等到这一批人全部交活**再告诉组长结果，组长不用一遍遍去问"做完没"；
> - `board`：看一眼看板现状；
> - `integrate`：把几个开发的改动合到一起，看合不合得上；
> - `apply_integration`：确认没问题后，把合好的成果正式放回你的项目文件夹，这一步一定会问你同不同意。
> 旧的 `delegate` 还能用，只是内部换成了新机制。

> 📖 **本节术语**
> - **文本工具协议**：模型用 `<tool>...</tool>` 这种文字格式表示要调用的工具，pico 再解析出来执行。
> - **阻塞调用**：调用之后要等它完全结束才会返回，期间调用方什么都做不了。
> - **轮询**：每隔一会儿去问一次"好了没"。

---

## 7. 上下文交接：Brief 和 Result

### 7.1 Brief（父 → 子）

由 HandoffBuilder 生成，**不再直接复制父 agent 的历史**，内容包括：

- `goal`、`acceptance`、`verify`
- 约束：`write_scope`、禁止事项、需要遵守的接口约定
- 相关文件摘要：取自父 memory 的 `file_summaries`，带新鲜度校验，过期的不传
- `inputs.facts`
- 依赖任务的 `Result.summary`
- 重试时：上一次的 `stop_reason` 和 `error`
- 总长度：不超过子 agent prompt 预算的 15%

### 7.2 Result（子 → 父）

子 agent 的 `<final>` 必须是下面这个结构的 JSON：

```json
{
  "status": "done | blocked | failed",
  "summary": "不超过 600 字",
  "findings": [{"claim": "...", "evidence": "pico/runtime.py:592"}],
  "changed_files": ["..."],
  "tests": [{"cmd": "pytest tests/test_x.py -q", "exit_code": 0}],
  "open_questions": ["..."],
  "proposed_memory": ["Decision: ..."],
  "confidence": 0.8
}
```

- **不完全相信模型自述**：`changed_files` 用 worktree 的真实改动覆盖；`tests` 用 trace 里的真实退出码核对。
- JSON 解析失败：先给一次带示例的重试提示；还失败就降级为 `{"status": "failed", "summary": 截断后的原始文本}`，`stop_reason=result_parse_failed`。
- 父历史里每个任务只写一条压缩后的 Result（不超过 1500 字符）。

> 💬 **白话解释**
> **Brief 是组长给组员的"任务交接单"**：要做什么、怎么验收、能动哪些文件、相关文件的要点（过时的不给，免得误导）、已经确认的事实、前面同事的结论；如果是重做，还会写明上次为什么没通过。交接单有长度限制，不会把组员的"记事本"一上来就塞满。
> **Result 是组员交回的"工作报告"**，按固定格式填：做成没、一段总结、发现了什么（每条都要附上"在哪个文件第几行"作为证据）、改了哪些文件、跑了哪些测试、还有什么疑问、建议记住哪些长期结论、自己有几成把握。
> 系统会核实报告里能核实的部分（改了哪些文件、测试结果）。报告格式写错，给一次重写机会，再错就按"失败"处理。

> 📖 **本节术语**
> - **HandoffBuilder**：负责生成 Brief 的模块。
> - **file_summaries**：pico memory 里"读过的文件 → 简短摘要"的记录。
> - **evidence（证据）**：`文件路径:行号`，方便核对结论。
> - **proposed_memory**：子 agent 建议写入持久记忆的内容，由组长决定是否采纳。
> - **confidence（置信度）**：0～1 之间的数，表示有多大把握。
> - **降级**：理想方案行不通时退而求其次，保证流程能继续。

---

## 8. 隔离与合并（worktree 和 branch）

### 8.1 先讲清楚几个 git 概念

| 概念 | 是什么 | 打个比方 |
|---|---|---|
| **仓库（repository）** | 一个被 git 管理的项目：所有文件 + 全部修改历史（历史存在 `.git` 文件夹里） | 一本书的原稿，加上从第一版到现在的所有修订记录 |
| **commit（提交）** | 某一时刻所有文件的完整快照，有一个编号（如 `abc1234`） | 某一天存档的一整版书稿 |
| **branch（分支）** | 从某个 commit 开始的一条**独立修改线**。在分支上提交新的 commit，不会影响别的分支 | 从"10 月 8 日版"复印一份，单独在上面修改，形成一条"小王修订版"的路线 |
| **工作区（working directory）** | 你在硬盘上看到、能编辑的那份文件，内容对应某个分支的当前状态 | 摊在你桌上、正在改的那份稿子 |
| **worktree** | 同一个仓库**额外开出来的工作区**：另一个文件夹，签出（checkout）另一个分支。它和主工作区共享同一份 `.git` 历史，不用复制整个仓库 | 给每个人单独一张桌子、一份复印件，大家共用同一个档案柜 |
| **merge（合并）** | 把一个分支的改动合进另一个分支 | 把小王修订版的改动誊到总稿上 |
| **合并冲突（conflict）** | 两个分支改了同一个文件的同一处，git 不知道保留哪一边 | 小王和小李都改了第 3 页第 2 段，誊稿的人不知道听谁的 |

**为什么非要用 worktree**：普通情况下一个仓库只有一个工作区，同一时间只能处在一个分支上。如果三个 worker 都直接改主工作区（你的 `E:\Github_program\pico`）：
- 大家同时写文件会互相覆盖；
- 跑测试时看到的是别人改了一半的代码；
- 某个人改坏了，很难只撤回他那一份；
- 你自己的文件也会被改得乱七八糟。

用 worktree 之后，每个 worker 有自己的文件夹和自己的分支，改坏了也只影响他自己那一份。

### 8.2 在 pico 里的样子

**文件夹**：

```
E:\Github_program\pico\                 ← 主工作区：你平时用的文件夹（比如在 main 分支）
├─ pico\ ...
├─ tests\ ...
└─ .pico\worktrees\                     ← 已在 .gitignore 里，不会被提交
   ├─ t2\                               ← worker t2 的工作区（分支 pico/<run>/t2）
   ├─ t3\                               ← worker t3 的工作区（分支 pico/<run>/t3）
   ├─ t4\                               ← worker t4 的工作区（分支 pico/<run>/t4）
   └─ _integration\                     ← 合并专用工作区（分支 pico/<run>/integration）
```

**分支关系**：

```
base_commit (abc1234)
   │
   ├── pico/<run>/t2 ：改 context_manager.py ──────┐
   ├── pico/<run>/t3 ：新增 test_context_budget.py ─┼──► pico/<run>/integration ──► reviewer 跑测试
   └── pico/<run>/t4 ：改 README.md ───────────────┘                                 │ 通过
                                                                                     ▼
                                                              apply_integration()（审批）→ 应用回主工作区
```

### 8.3 流程

**Explorer**：直接用主工作区，靠工具白名单保证只读。

**Worker**：
1. 执行 `git worktree add .pico/worktrees/<task> -b pico/<run>/<task> <base_commit>`（含义见下方术语）。主工作区有未提交的改动时，先把这些改动应用到 worktree，并在 trace 里记一条警告。
2. 子 `Pico` 的 `repo_root` 设成这个 worktree。现有的路径校验会把它所有读写都限制在 worktree 里，不需要额外改代码。
3. 审批：worktree 内的 `write_file` / `patch_file` 自动批准（只影响隔离副本）；`run_shell` 仍走 ApprovalBroker。
4. 子 agent 结束后，runtime 在 worktree 里把改动提交成 commit（`git add -A && git commit`），这样分支上才有东西可以合并。
5. **不是 git 仓库时**：降级为串行执行 worker，直接写主工作区，沿用现有的工作区快照 diff。

**Integrator**：
1. 按依赖顺序把 worker 分支 `git merge --no-ff` 到 `_integration`。
2. 有冲突：执行 `git merge --abort` 撤销这次合并，把冲突文件列表返回给组长，由组长决定是否派一个 worker 专门解决。
3. 合并前先做越界检查，`scope_violation` 的任务默认不合并。
4. 合并完成、reviewer 通过后，才能通过 `apply_integration()` 应用回主工作区，这一步需要审批。
5. 清理：成功的 worktree 自动删除（`git worktree remove`）；失败的保留以便排查，另提供 `pico worktrees prune` 手动清理。Windows 要注意路径长度和文件被占用的问题。

**并行的前提是接口已经确定**：比如"实现函数"和"写测试"，只有 Brief 里已经写明函数签名时才能并行；否则写测试要依赖实现任务。这条规则写进组长的提示词。

> 💬 **白话解释**
> 用一个具体的比方把整节串起来：
> 1. 你的项目文件夹是"总稿"。开工前系统记下总稿当前版本（`base_commit`）。
> 2. 每个开发（worker）领到一张**自己的桌子**（worktree，就是 `.pico\worktrees\t2` 这样的文件夹），桌上放着同一版总稿的复印件。他在上面怎么改都不会影响别人，也不会影响你的总稿。
> 3. 每张桌子的修改都记在**他自己的修订线**上（branch，比如 `pico/<run>/t2`）。
> 4. 大家改完后，合稿编辑（Integrator）在**专门的合稿桌**（`_integration`）上把几条修订线合到一起（merge）。如果两人改了同一处（冲突），就停下来交给组长处理；如果有人改了分工之外的地方（越界），他那份先不合。
> 5. 合好后，验收（reviewer）在合稿桌上跑测试。
> 6. 测试通过、你也同意后，才把合好的稿子放回你的总稿。
> 所以在整个过程中，**你自己的项目文件夹一直不会被动**，直到最后一步你点头为止。

> 📖 **本节术语**
> - **签出（checkout）**：把某个分支的内容放到工作区里，让你能看到和编辑。
> - **`git worktree add <路径> -b <新分支名> <起点commit>`**：在 `<路径>` 新建一个工作区，同时以 `<起点commit>` 为起点新建分支 `<新分支名>` 并签出到这个工作区。
> - **repo_root**：pico 认定的项目根目录，所有文件操作都被限制在它下面。
> - **`git add -A && git commit`**：把所有改动（包括新文件）存成一个新 commit。
> - **`--no-ff`**：合并时一定生成一个"合并提交"，以后能清楚看出"这里合进了 t2 的改动"。
> - **`git merge --abort`**：放弃这次合并，恢复到合并前的状态。
> - **`git worktree remove` / `prune`**：删除不再需要的 worktree / 清理残留记录。
> - **函数签名**：函数叫什么、接收哪些参数、返回什么，比如 `estimate_tokens(text) -> int`。
> - **路径长度问题**：Windows 默认限制完整路径不超过约 260 个字符，嵌套太深会出错。
> - **文件被占用**：Windows 上文件被某个程序打开时不能删除，比如之前遇到的 `pico.exe`。

---

## 9. 并发、预算与安全

| 机制 | 设计 |
|---|---|
| Scheduler | 用 `ThreadPoolExecutor`。调用模型 API 时大部分时间在等网络，同步调用放到线程里就能并行。每个子 agent 有独立的 Pico、session、memory 和 run 目录 |
| ModelClientFactory | **每个子 agent 新建一个 client 实例**，解决共用 `last_completion_metadata` 的竞争；测试里按 `task_id` 分发不同剧本 |
| BudgetLedger | 全局上限：`max_total_steps`、`max_total_prompt_tokens`（估算）、`max_tasks`、`max_attempts`。子 agent 每走一步前先申请额度，额度用完就以 `stop_reason=budget_exhausted` 结束 |
| 子 agent 的预算 | 组长的 context profile × 角色的 `budget_ratio` |
| ApprovalBroker | 一把锁 + 一个队列；提示里带上来源，例如 `[t2/worker] approve run_shell ...?`；`auto` / `never` 模式不排队 |
| 持久记忆 | 子 agent 设 `durable_memory_writes=False`，只能通过 `proposed_memory` 提出建议，由组长决定是否写入。顺带修掉"只读子 agent 能写持久记忆"的问题 |
| 子 session | 放到 `.pico/sessions/children/`，并记录 `parent_session_id`；`latest()` 只看顶层目录 |
| 取消 | 每个子 agent 每一步开始前检查共享的 `cancel_event`；组长超预算或出错时统一取消 |
| 递归 | 子 agent 没有任何编排类工具，深度固定不超过 1 |

> 💬 **白话解释**
> 这一节讲"人多了怎么不乱、不超支、不出事"：
> - **怎么同时干**：几个组员同时打电话问模型，大部分时间都在等回复，所以用几个线程同时等就行，不用开多个程序；
> - **每人一部电话**：每个组员有自己的 model_client，账单不会记混；
> - **财务管账**：整个团队总共能用多少步、多少 token、开多少张任务卡、重试几次，都有上限。每走一步先"报账"，钱花完就停；
> - **审批排队**：多人同时要审批时，一个一个问你，每次都说明是谁在问；
> - **长期记忆只有组长能写**：组员只能"建议记住什么"；
> - **组员的会话记录单独放**：不会和组长的混在一起，`--resume latest` 不会接错；
> - **一键叫停**：组长按下"取消"，所有组员在下一步开始前都会停下；
> - **组员不能再招人**。

> 📖 **本节术语**
> - **IO 密集**：大部分时间花在等待输入输出（网络、磁盘），而不是计算上。这种任务用线程并行效果很好。
> - **竞争（race condition）**：多个线程同时读写同一份数据，结果取决于谁先谁后，导致数据错乱。
> - **额度**：预算里还剩下、可以申请使用的部分。
> - **锁**：同一时刻只允许一个线程进入某段代码，其他线程排队等。
> - **cancel_event**：一个所有线程都能看到的"停止信号"，一旦被设置，大家就各自停下。
> - **parent_session_id**：子会话记录里指向父会话的编号。

---

## 10. 可观测性

- 父 trace 新增事件：`tasks_planned`、`task_started`、`task_finished`、`task_retried`、`integrate_attempted`、`merge_conflict`、`scope_violation`、`budget_exhausted`、`task_cancelled`。
- 子 run 的 task_state 和 report 新增：`parent_run_id`、`task_id`、`role`。
- `board.json` 本身就是这次协作的快照；有了 `started_at` / `finished_at`，可以画出各任务的甘特图。
- 父 report 新增 `multi_agent` 汇总：任务数、并发峰值、总步数、总估算 token、各角色耗时、冲突次数、重试次数、越界次数，以及**加速比 = 各任务耗时之和 ÷ 实际总耗时**。
- CLI 新增：REPL 命令 `/tasks`；`pico runs tree <run_id>` 查看运行树。

> 💬 **白话解释**
> "可观测性"就是**事后能不能看清楚发生了什么**。这里做了几件事：每个关键动作都记一条日志；组员的日志能顺藤摸瓜找到组长那次；看板文件本身就是整个协作过程的记录；最后出一份汇总，写清楚开了几个任务、最多同时几个、花了多少、合并冲突几次、比一个人干快了几倍。还加了两个命令方便你在终端里直接查看。

> 📖 **本节术语**
> - **可观测性**：通过日志、指标等手段了解系统内部发生了什么的能力。
> - **trace 事件**：日志里的一条记录，如"t2 开始了"。
> - **甘特图**：横轴是时间、每行一个任务、用横条表示任务起止时间的图，一眼能看出哪些任务在同时进行。
> - **并发峰值**：同一时刻最多有几个任务在跑。
> - **REPL**：pico 的交互模式，输入一句、回答一句，循环进行。

---

## 11. 协作模式

以下模式作为编排策略写进组长的提示词：

| 模式 | 流程 | 适用场景 |
|---|---|---|
| A. 并行探查 | 多个 explorer 分别查不同模块，组长汇总 | 分析仓库、定位 bug |
| B. 规划 → 实现 → 审查（主流程） | plan_tasks → 并行 worker → integrate → reviewer → apply | 跨多个文件的功能开发 |
| C. Best-of-N | 同一个 worker 任务开 N 份（`variant_of` 指向原任务，可用不同模型或温度），reviewer 跑 verify，选通过且改动最小的那份，其余 `cancelled`、`stop_reason=variant_not_selected` | 难修的 bug。成本是 N 倍，默认关闭 |
| D. 修复循环 | verify 或 reviewer 失败后，把失败信息写进新 Brief 交给原 worker 重试，次数不超过 `max_attempts` | 测试没过时 |

**不该用多 agent 的情况**：只涉及 2 个以内文件，或者预计 3 步以内能完成，就直接由单 agent 完成。这条规则写进提示词，并在评测里验证。

> 💬 **白话解释**
> 四种"打法"：
> - **A**：几个调研员分头查不同的地方，组长汇总；
> - **B**：最常用的流程。先定计划，几个开发同时写，合在一起，验收跑测试，你同意后落地；
> - **C**：难题让几个人各做一版，挑测试通过、而且改动最少的那份。很费钱，默认不开；
> - **D**：测试没过，就把错误信息交给原来的开发让他改，改的次数有上限。
> 还有一条很重要：**小事别开会**。简单任务一个人干更快更省，硬拆成多人反而浪费。

> 📖 **本节术语**
> - **温度（temperature）**：控制模型回答随机程度的参数，越高越发散，越低越稳定。
> - **改动最小**：diff 的行数最少，通常意味着更不容易引入额外问题。

---

## 12. 示例流程

任务："给 ContextManager 加 token 预算并补测试，再更新 README"

1. `plan_tasks`：
   - t1：explorer，查现有的预算逻辑和测试
   - t2：worker，write_scope `[pico/context_manager.py, pico/context_budget.py]`，依赖 t1，verify 为相关测试
   - t3：worker，write_scope `[tests/test_context_budget.py]`，依赖 t1，Brief 里写明接口签名
   - t4：worker，write_scope `[README.md]`，依赖 t1
   - t5：reviewer，依赖 t2、t3、t4，verify 为 `python -m pytest tests -q`
2. `run_tasks("ready")` → 执行 t1
3. `run_tasks("ready")` → t2、t3、t4 并行（三者 write_scope 不重叠）
4. `integrate([t2, t3, t4])` → 合并到 `_integration`
5. `run_tasks([t5])` → reviewer 在 `_integration` 里跑全部测试
6. 通过 → `apply_integration()`（审批）→ 输出 final

> 💬 **白话解释**
> 先派一个调研员把现状查清楚；然后三个开发同时动手，一个改核心代码、一个写测试、一个改文档，因为各管各的文件，可以同时进行；三份改动合到一起后，让验收员把全部测试跑一遍；都通过了，问你一句"可以放回项目吗"，你同意就完成。

---

## 13. 评测

新增 benchmark 层 `multi_agent`，两套：FakeModelClient 剧本版（回归测试）和真实模型版（效果对比）。

| 维度 | 指标 |
|---|---|
| 正确性 | 与单 agent 对比的任务成功率（verifier 判定） |
| 效率 | 加速比、总 token 成本比 |
| 协作质量 | 合并冲突率、越界次数、重试次数、Result 解析失败率 |
| 安全不变量 | 子 agent 不能写持久记忆；可并行的 worker 之间 write_scope 不重叠；审批串行；越界不被合并；预算用完能停；崩溃后 board 能恢复；子 agent 无法再派生 |
| 消融实验 | 并发数 1 vs 3；Brief vs 直接塞父历史；有无 reviewer；有无 best-of-N |

> 💬 **白话解释**
> 怎么证明多人协作真的有用？
> - **做得对不对**：同一批题，多人版和单人版谁做对的多；
> - **快不快、贵不贵**：快了几倍，多花了几倍 token；
> - **协作顺不顺**：多久打一次架（冲突），多少人越界，多少次要返工；
> - **规矩有没有被破坏**：这些"绝对不能发生"的事，在测试里一项一项验证；
> - **每个设计值不值**：把某个设计关掉对比一下，看它到底贡献了多少。

> 📖 **本节术语**
> - **benchmark**：固定的一组评测题。
> - **回归测试**：改代码后重新跑一遍，确认原来好的功能没被改坏。
> - **verifier**：判断一道题是否做对的检查程序。
> - **不变量**：无论什么情况都必须成立的规则。
> - **消融实验**：去掉或替换某个部分后对比效果，用来衡量这个部分的贡献。

---

## 14. 实施路线

| 阶段 | 内容 | 落地位置 | 状态 |
|---|---|---|---|
| **Phase 0 前置修复** | 批处理结果重复写进历史；memory 双写和加锁；ModelClientFactory；子 agent 禁写持久记忆；子 session 隔离和父子关联 | `agent_loop.py`、`tool_executor.py`、`runtime.py`、`session_store.py` | ✅ 已实现 |
| **Phase 1 角色 + 结构化交接** | 角色注册表、Brief/Result、board.json、`plan_tasks` / `run_tasks`、trace 事件 | `pico/agents/registry.py`、`pico/agents/builtin/*.md`、`pico/orchestration/task_board.py`、`handoff.py`、`verify.py` | ✅ 已实现 |
| **Phase 2 并行与管控** | 并行调度、BudgetLedger、ApprovalBroker、取消 | 调度在 `orchestration/orchestrator.py`（未单独拆 `scheduler.py`）、`budget.py`、`approval.py` | ✅ 已实现 |
| **Phase 3 并行写** | worktree 管理、Integrator、write_scope 校验、verify、reviewer、`apply_integration` | `orchestration/worktree.py`、`integrator.py` | ✅ 已实现 |
| **Phase 4 进阶** | best-of-N、multi_agent 评测层、`/tasks`、`--runs-tree`、汇总指标 | `orchestration/metrics.py`、`evaluation/multi_agent.py`、`cli.py` | ✅ 已实现 |

落地与本稿的主要差异：
- 并行调度没有单独拆成 `scheduler.py`，而是作为 `Orchestrator._schedule` 的 wave 式调度实现。
- 新增了设计稿未单列的 `orchestration/metrics.py`（汇总指标）和 `orchestration/verify.py`（跨平台验收命令执行）。
- best-of-N 变体 id 用 `-vN` 后缀（如 `sol-v1`），因为 task_id 字符集限定为 `[a-z0-9_-]`。
- `delegate` 保留旧接口不变（未改走编排流程），以保持向后兼容。
- 测试：`tests/test_orchestration.py`（Phase 1）、`tests/test_orchestration_parallel.py`（Phase 2–4），随主套件全绿。

> 💬 **白话解释**
> 分五步走，每一步做完都能单独测试、单独用：先把会被"多人同时干活"放大的老毛病修好；再让"组长 + 组员"的流程一次一个人地跑通；然后让只看不改的调研员能同时干；再让改代码的开发也能同时干；最后加上"多版本择优"和评测。

---

## 15. 风险与取舍

- **成本**：多 agent 的总 token 通常是单 agent 的好几倍。缓解：默认并发 3、explorer 小预算、简单任务不拆、全局预算上限。
- **文本协议下 JSON 容易写错**：用重试和降级兜底；接入原生 tool calling 后可改成结构化输出。
- **弱模型当组长效果差**：用 Ollama 小模型时默认关闭编排工具，只保留 explorer。
- **`run_tasks` 会卡住组长的循环**：组长没法中途调整计划，v1 可以接受。
- **依赖 git**：非 git 仓库降级为串行写；Windows 注意路径长度和文件占用。
- **星型拓扑的代价**：组员之间不能直接商量，换来的是可审计、不会死锁、预算可控。

> 💬 **白话解释**
> 任何设计都有代价：人多了更贵，所以要限人数、限预算、小事不拆；模型可能写错报告格式，所以给重写机会；能力弱的模型当不好组长，所以那种情况只让它派调研员；组长"派完活就干等"，中途不能改主意，先这样；不用 git 的项目就退回一个一个来；组员不能私聊，但换来了"什么都有记录、不会卡死、钱花得清楚"。

> 📖 **本节术语**
> - **原生 tool calling**：模型 API 自带的结构化工具调用方式，模型直接返回结构化数据，而不是写一段文字再由 pico 解析。

---

## 附录 A：术语表

| 术语 | 解释 |
|---|---|
| agent | 由大模型驱动、能自己循环"思考 → 调用工具 → 看结果"的程序 |
| Orchestrator（编排者） | 主 agent，负责拆任务、派活、汇总，相当于组长 |
| 子 agent | 被派出去干一小块活的 agent，相当于组员 |
| Explorer / Worker / Reviewer / Planner | 调研员（只读）/ 开发（可写）/ 验收（跑测试）/ 方案设计（只读，可选） |
| TaskBoard | 任务看板，存成 `board.json` |
| Scheduler | 排班员，决定什么时候开始哪个任务 |
| Integrator | 合稿编辑，负责合并各分支并做检查 |
| ApprovalBroker | 审批窗口，把审批请求排队 |
| BudgetLedger | 预算账本，管理全局上限 |
| Brief / Result | 任务交接单 / 工作报告，都是固定格式的 JSON |
| HandoffBuilder | 生成 Brief 的模块 |
| harness | 包在模型外面的控制程序，也就是 pico 本身 |
| runtime | pico 程序自身的逻辑（区别于模型的决定） |
| prompt | 每次发给模型的完整输入 |
| prefix | prompt 最前面那段相对固定的内容 |
| token | 模型计量文字的单位 |
| 上下文窗口 | 模型一次最多能看的 token 数 |
| 系统提示 | 对话开头规定模型身份和规则的文字 |
| 工具白名单 / allowed_tools | 明确允许使用的工具列表 |
| delegate | pico 现有的派子 agent 工具 |
| read_only | 只读模式 |
| approval_policy | 审批策略：ask / auto / never |
| max_steps / max_attempts | 最多工具步数 / 最多尝试次数 |
| depth | 委派层级，组长是 0 |
| session | 一次连续会话及其状态（`.pico/sessions/`） |
| run | 一次 `ask()` 的执行（`.pico/runs/<run_id>/`） |
| trace / report | 一次 run 的事件日志 / 总结报告 |
| checkpoint | pico 已有的进度存档机制 |
| 持久记忆（durable memory） | 跨会话长期保存的记忆（`.pico/memory/`） |
| file_summaries | memory 里"文件 → 摘要"的记录 |
| model_client | 调用模型 API 的对象 |
| FakeModelClient | 测试用的假模型，按剧本返回 |
| usage | 一次模型调用的用量统计 |
| 实例 / 实例属性 | 由类创建的具体对象 / 挂在对象上的数据 |
| 进程 / 线程 / 主线程 | 运行中的程序 / 程序里能同时执行的执行流 / 程序启动时的那个线程 |
| ThreadPoolExecutor | Python 自带的线程池 |
| 并行 / 串行 / 并发数 | 同时做 / 依次做 / 同一时刻最多做几个 |
| IO 密集 | 大部分时间在等网络或磁盘 |
| 同步 IO / 阻塞调用 | 发请求后原地等结果 / 调用结束前不返回 |
| 轮询 | 隔一段时间问一次"好了没" |
| 竞争（race condition） | 多线程同时改同一份数据导致错乱 |
| 锁 / 队列 | 让线程排队进入 / 先进先出的排队结构 |
| 线程安全 | 多线程同时使用也不出错 |
| cancel_event | 所有线程共享的停止信号 |
| 原子写 | 先写临时文件再整体替换，避免写一半 |
| JSON / schema | 结构化文本格式 / 数据结构约定 |
| 星型拓扑 / 网状 | 只和中心相连 / 人人互连 |
| 死锁 | 互相等待，谁都动不了 |
| 环 / 拓扑排序 | 依赖绕成圈 / 把依赖排成合法顺序的算法 |
| 就绪 / 终态 | 可以开始了 / 不会再变的最终状态 |
| 仓库（repository） | 被 git 管理的项目：文件 + 全部历史 |
| commit | 某一时刻所有文件的快照，有编号 |
| HEAD | 当前所在的 commit |
| base_commit | 本次协作所有 worker 的共同起点 commit |
| branch（分支） | 从某个 commit 出发的一条独立修改线 |
| 工作区（working directory） | 硬盘上能看到、能编辑的那份文件 |
| 主工作区 | 你平时用的项目文件夹 |
| worktree | 同一仓库额外开的工作区：另一个文件夹签出另一个分支，共享同一份历史 |
| 集成 worktree（`_integration`） | 专门用来合并所有改动的 worktree |
| 签出（checkout） | 把某分支内容放到工作区 |
| merge / `--no-ff` | 合并分支 / 合并时一定生成合并提交 |
| 合并冲突 | 两边改了同一处，git 无法自动决定 |
| `git merge --abort` | 放弃这次合并 |
| 已跟踪文件 / 未提交的改动 | 已被 git 管理的文件 / 改了但还没存成 commit |
| write_scope | 任务声明"只会改这些文件" |
| scope_violation（越界写入） | 实际改动超出了 write_scope |
| canonicalize_path | 把路径统一成标准写法 |
| IGNORED_PATH_NAMES | pico 已有的忽略目录列表 |
| verify | 任务结束后自动执行的验收命令 |
| verifier | 判断题目是否做对的检查程序 |
| 退出码 | 命令结束时返回的数字，0 为成功 |
| shell | 命令行解释器（cmd / PowerShell / bash） |
| sha256 / 新鲜度 | 文件指纹算法 / 文件生成摘要后有没有再被改过 |
| stop_reason / error | 结束原因代码 / 错误详情文字 |
| attempts_history | 每次尝试的记录列表 |
| model / model_resolved | 规划时想用的模型 / 实际用的模型 |
| variant_of | best-of-N 候选所指向的原任务 |
| best-of-N | 同一任务做 N 次，挑最好的 |
| 温度 | 控制模型回答随机程度的参数 |
| proposed_memory | 子 agent 建议写入长期记忆的内容 |
| confidence | 置信度，0～1 |
| 降级 | 理想方案不行时退而求其次 |
| UTC / ISO 8601 | 协调世界时（香港时间 = UTC + 8） / 国际标准时间写法 |
| 墙钟耗时 / 毫秒 | 现实中经过的时间 / 千分之一秒 |
| 加速比 | 各任务耗时之和 ÷ 实际总耗时 |
| 可观测性 | 事后看清系统内部发生了什么的能力 |
| 甘特图 | 用横条表示各任务起止时间的图 |
| 并发峰值 | 同一时刻最多运行的任务数 |
| REPL | 交互模式：输入一句、回答一句 |
| benchmark / 回归测试 | 固定评测题 / 改动后确认旧功能没坏 |
| 不变量 | 任何情况下都必须成立的规则 |
| 消融实验 | 去掉某部分对比效果，衡量它的贡献 |
| 原生 tool calling | 模型 API 自带的结构化工具调用 |
| frontmatter | Markdown 开头 `---` 之间的配置 |
| AGENTS.md | 给 coding agent 写项目说明的约定文件 |
| 函数签名 | 函数名、参数和返回值的定义 |
