"""Agent 运行时核心逻辑。
感知 ->决策 -》行动-》记录
Pico 就是包在模型外面的控制循环：负责组 prompt、解析模型输出、
校验并执行工具、写 trace、更新工作记忆，以及在合适的时候停下来。
"""

import json
import hashlib
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

from . import checkpoint as checkpointlib
from .features import memory as memorylib
from . import security as securitylib
from .context_manager import ContextManager
from .checkpoint import CHECKPOINT_NONE_STATUS
from .prompt_prefix import build_prompt_prefix, tool_signature
from .run_store import RunStore
from .security import REDACTED_VALUE
from .session_store import SessionStore
from .tool_context import ToolContext
from .tool_executor import ToolExecutor
from . import tools as toolkit
from .workspace import IGNORED_PATH_NAMES, MAX_HISTORY, MAX_TOOL_OUTPUT, WorkspaceContext, clip, now

DEFAULT_SHELL_ENV_ALLOWLIST = ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "PATH", "PWD", "SHELL", "TERM", "TMPDIR", "TMP", "TEMP", "USER")
DEFAULT_FEATURE_FLAGS = {
    "memory": True,
    "relevant_memory": True,
    "context_reduction": True,
    "prompt_cache": True,
}
DURABLE_MEMORY_INTENT_PATTERN = re.compile(r"(?i)\b(capture|remember|save|store|persist|note)\b")
DURABLE_MEMORY_INTENT_ZH_PATTERN = re.compile(r"(记住|保存|记录|沉淀|长期记忆|持久记忆)")
DURABLE_MEMORY_LINE_PATTERNS = (
    ("project-conventions", re.compile(r"(?i)^Project convention:\s*(.+)$")),
    ("key-decisions", re.compile(r"(?i)^Decision:\s*(.+)$")),
    ("dependency-facts", re.compile(r"(?i)^Dependency:\s*(.+)$")),
    ("user-preferences", re.compile(r"(?i)^Preference:\s*(.+)$")),
    ("project-conventions", re.compile(r"^项目约定：\s*(.+)$")),
    ("key-decisions", re.compile(r"^决策：\s*(.+)$")),
    ("dependency-facts", re.compile(r"^依赖：\s*(.+)$")),
    ("user-preferences", re.compile(r"^偏好：\s*(.+)$")),
)
SECRET_SHAPED_TEXT_PATTERN = re.compile(r"(?i)(\b(api[_ -]?key|token|secret|password)\b|sk-[A-Za-z0-9_-]{6,})")

__all__ = ["Pico", "SessionStore"]


class Pico:
    def __init__(
        self,
        model_client,
        workspace,
        session_store,
        session=None,
        run_store=None,
        approval_policy="ask",
        max_steps=6,
        max_new_tokens=512,
        depth=0,
        max_depth=1,
        read_only=False,
        shell_env_allowlist=None,
        secret_env_names=None,
        feature_flags=None,
        allowed_tools=None,
        context_profile=None,
        model_client_factory=None,
        durable_memory_writes=True,
        parent_run_id="",
        role="",
        cancel_event=None,
        approval_broker=None,
        orchestration_config=None,
        collab=None,
    ):
        self.model_client = model_client
        # 并行只读批处理时多个线程会同时更新 memory/session，用可重入锁串行化这些写操作。
        self._state_lock = threading.RLock()
        self.workspace = workspace
        self.root = Path(workspace.repo_root)
        self.session_store = session_store
        self.approval_policy = approval_policy
        self.max_steps = max_steps
        self.max_new_tokens = max_new_tokens
        self.depth = depth
        self.max_depth = max_depth
        self.read_only = read_only
        self.shell_env_allowlist = tuple(shell_env_allowlist or DEFAULT_SHELL_ENV_ALLOWLIST)
        self.secret_env_names = {str(name).upper() for name in (secret_env_names or ())}
        self.feature_flags = dict(DEFAULT_FEATURE_FLAGS)
        if feature_flags:
            self.feature_flags.update({str(key): bool(value) for key, value in feature_flags.items()})
        self.allowed_tools = self._normalize_allowed_tools(allowed_tools)
        # 上下文预算配置；None 表示 compact（旧版固定预算）。
        # 工具输出上限也跟着 profile 走：prompt 能装下更多内容时，单次读文件/跑命令的结果也不该还卡在 4000 字符。
        self.context_profile = context_profile
        self.tool_output_limit = int(context_profile.tool_output_chars) if context_profile is not None else MAX_TOOL_OUTPUT
        # 每个子 agent 应新建自己的 model_client（见 spawn_delegate / 编排层），
        # 避免父子共用实例时 last_completion_metadata 与 FakeModelClient.outputs 串扰。
        self.model_client_factory = model_client_factory
        # 子 agent 只能通过 Result.proposed_memory 建议记忆，不能直接写持久记忆。
        self.durable_memory_writes = bool(durable_memory_writes)
        self.parent_run_id = str(parent_run_id or "")
        self.role = str(role or "")
        # 协作取消信号：orchestrator 超预算/出错时置位，子 agent 在每轮开始检查后停机。
        self.cancel_event = cancel_event
        # 并行审批串行化（仅 approval_policy=="ask" 时用到）。
        self.approval_broker = approval_broker
        # 编排配置：{max_concurrency, max_total_steps, max_total_prompt_tokens, max_tasks, max_attempts}
        self.orchestration_config = dict(orchestration_config or {})
        # 协作句柄（CollabHandle）：仅子 agent 持有，提供黑板/信箱工具。
        self.collab = collab
        self.run_store = run_store or RunStore(Path(workspace.repo_root) / ".pico" / "runs")
        self.session = session or {
            "id": datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "created_at": now(),
            "workspace_root": workspace.repo_root,
            "history": [],
            "memory": memorylib.default_memory_state(),
        }
        self._ensure_session_shape()
        self.memory = memorylib.LayeredMemory(
            self.session.setdefault("memory", memorylib.default_memory_state()),
            workspace_root=self.root,
        )
        self.session["memory"] = self.memory.to_dict()
        self.tools = self._apply_tool_allowlist(self.build_tools())
        self.tool_executor = ToolExecutor(self)
        self.prefix_state = self.build_prefix()
        self.prefix = self.prefix_state.text
        self.context_manager = ContextManager(self, profile=context_profile)
        self.resume_state = self.evaluate_resume_state()
        self.session_path = self.session_store.save(self.session)
        self.current_task_state = None
        self.current_run_dir = None
        self.last_prompt_metadata = {}
        self.last_completion_metadata = {}
        self.last_durable_promotions = []
        self.last_durable_rejections = []
        self.last_durable_superseded = []
        self._last_tool_result_metadata = {}
        self._last_prefix_refresh = {
            "workspace_changed": False,
            "prefix_changed": False,
        }
        # 多 agent 编排器，仅 depth==0 懒加载（见 orchestrator / build_tools）。
        self._orchestrator = None

    @classmethod
    def from_session(cls, model_client, workspace, session_store, session_id, **kwargs):
        return cls(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            session=session_store.load(session_id),
            **kwargs,
        )

    def _ensure_session_shape(self):
        self.session.setdefault("history", [])
        self.session.setdefault("memory", memorylib.default_memory_state())
        checkpoints = self.session.setdefault("checkpoints", {})
        if not isinstance(checkpoints, dict):
            checkpoints = {}
            self.session["checkpoints"] = checkpoints
        checkpoints.setdefault("current_id", "")
        checkpoints.setdefault("items", {})
        runtime_identity = self.session.setdefault("runtime_identity", {})
        if not isinstance(runtime_identity, dict):
            self.session["runtime_identity"] = {}
        resume_state = self.session.setdefault("resume_state", {})
        if not isinstance(resume_state, dict):
            self.session["resume_state"] = {}

    def current_runtime_identity(self):
        return checkpointlib.current_runtime_identity(self)

    def checkpoint_state(self):
        return checkpointlib.checkpoint_state(self)

    def current_checkpoint(self):
        return checkpointlib.current_checkpoint(self)

    def invalidate_stale_memory(self):
        invalidated = self.memory.invalidate_stale_file_summaries()
        self.session["memory"] = self.memory.to_dict()
        return invalidated

    def evaluate_resume_state(self):
        return checkpointlib.evaluate_resume_state(self)

    def render_checkpoint_text(self):
        return checkpointlib.render_checkpoint_text(self)

    @staticmethod
    def remember(bucket, item, limit):
        if not item:
            return
        if item in bucket:
            bucket.remove(item)
        bucket.append(item)
        del bucket[:-limit]

    def build_tools(self):
        tools = toolkit.build_tool_registry(self.tool_context())
        # 编排类工具只给顶层 agent，子 agent 拿不到，从而无法再编排（委派深度 <= 1）。
        if self.depth == 0:
            tools.update(self._orchestration_tool_specs())
        # 协作工具（黑板/信箱）给持有 collab 句柄的子 agent：只共享数据，不含编排权。
        if self.collab is not None:
            tools.update(self._collaboration_tool_specs())
        return tools

    def _collaboration_tool_specs(self):
        collab = self.collab

        def _post(args):
            args = args or {}
            return collab.post_note(args.get("key", ""), args.get("text", ""),
                                    evidence=args.get("evidence", ""), kind=args.get("kind", "note"))

        def _read(args):
            args = args or {}
            return collab.read_notes(key_prefix=args.get("key_prefix", args.get("key", "")),
                                     limit=int(args.get("limit", 20) or 20))

        def _send(args):
            args = args or {}
            to_task = str(args.get("to_task", "")).strip()
            if not to_task:
                return "error: send_message requires 'to_task'"
            return collab.send_message(to_task, args.get("text", ""))

        def _inbox(args):
            return collab.check_inbox()

        return {
            "post_note": {"schema": {"key": "str", "text": "str", "evidence": "str?"}, "risky": False,
                          "description": "Post a finding/decision/interface note to the shared blackboard for other sub-agents to read.", "run": _post},
            "read_notes": {"schema": {"key_prefix": "str?"}, "risky": False,
                           "description": "Read notes from the shared blackboard, optionally filtered by key prefix.", "run": _read},
            "send_message": {"schema": {"to_task": "str", "text": "str"}, "risky": False,
                             "description": "Leave an async message in another task's inbox (non-blocking; no reply is waited for).", "run": _send},
            "check_inbox": {"schema": {}, "risky": False,
                            "description": "Read unread messages addressed to this task.", "run": _inbox},
        }

    def orchestrator(self):
        if self._orchestrator is None:
            from .orchestration import Orchestrator, BudgetLedger
            from .agents import RoleRegistry
            roles = RoleRegistry.load(user_dir=self.root / ".pico" / "agents")
            cfg = self.orchestration_config
            budget = BudgetLedger(
                max_total_steps=cfg.get("max_total_steps", 120),
                max_total_prompt_tokens=cfg.get("max_total_prompt_tokens", 2_000_000),
                max_tasks=cfg.get("max_tasks", 24),
                max_attempts=cfg.get("max_attempts", 5),
            )
            self._orchestrator = Orchestrator(
                self, roles,
                max_concurrency=cfg.get("max_concurrency", 3),
                budget=budget,
            )
        return self._orchestrator

    def _orchestration_tool_specs(self):
        def _plan(args):
            return self.orchestrator().plan_tasks((args or {}).get("tasks"))

        def _run(args):
            args = args or {}
            selector = args.get("task_ids", args.get("tasks", "ready"))
            mode = args.get("mode", "wave")
            return self.orchestrator().run_tasks(selector, mode=mode)

        def _amend(args):
            return self.orchestrator().amend_tasks((args or {}).get("tasks"))

        def _cancel(args):
            args = args or {}
            return self.orchestrator().cancel_tasks(args.get("task_ids", args.get("ids", [])), args.get("reason", ""))

        def _answer(args):
            args = args or {}
            return self.orchestrator().answer_need(args.get("task_id", ""), args.get("text", ""))

        def _board(args):
            return self.orchestrator().board_view()

        def _integrate(args):
            args = args or {}
            return self.orchestrator().integrate(args.get("task_ids", args.get("tasks", [])))

        def _apply(args):
            return self.orchestrator().apply_integration()

        def _select(args):
            args = args or {}
            return self.orchestrator().select_variant(args.get("group", args.get("variant_of", "")), args.get("winner", ""))

        return {
            "plan_tasks": {
                "schema": {"tasks": "list[task]"},
                "risky": False,
                "description": "Create or update the multi-agent task board. Each task: {task_id, role, goal, acceptance[], verify[], inputs{files[],facts[]}, depends_on[], write_scope[], max_attempts}.",
                "run": _plan,
            },
            "run_tasks": {
                "schema": {"task_ids": "list|'ready'", "mode": "'wave'|'all'"},
                "risky": False,
                "description": "Run one wave of ready tasks (mode='wave', default) or drain all ready tasks (mode='all'); returns a progress digest so you can adjust the plan between waves.",
                "run": _run,
            },
            "amend_tasks": {
                "schema": {"tasks": "list[task]"},
                "risky": False,
                "description": "Add new tasks or revise still-pending/blocked tasks' goal/acceptance/write_scope/depends_on. Done tasks cannot be changed.",
                "run": _amend,
            },
            "cancel_tasks": {
                "schema": {"task_ids": "list", "reason": "str"},
                "risky": False,
                "description": "Cancel pending/blocked tasks (and their dependents). Use to stop a wrong approach before replanning.",
                "run": _cancel,
            },
            "answer_need": {
                "schema": {"task_id": "str", "text": "str"},
                "risky": False,
                "description": "Answer a blocked task's open need and return it to pending so it can be retried with the answer in its brief.",
                "run": _answer,
            },
            "board": {
                "schema": {},
                "risky": False,
                "description": "Show the current task board.",
                "run": _board,
            },
            "integrate": {
                "schema": {"task_ids": "list"},
                "risky": False,
                "description": "Merge the given done worker branches into an integration worktree; reports conflicts or the integrated diff. (git repos only)",
                "run": _integrate,
            },
            "apply_integration": {
                "schema": {},
                "risky": False,
                "description": "Apply the integrated result back to the main workspace. Asks for approval and requires a passing reviewer (approval handled internally).",
                "run": _apply,
            },
            "select_variant": {
                "schema": {"group": "str", "winner": "str"},
                "risky": False,
                "description": "For a best-of-N variant group, keep the winner task and cancel the other done variants.",
                "run": _select,
            },
        }

    @staticmethod
    def _normalize_allowed_tools(allowed_tools):
        if allowed_tools is None:
            return None
        normalized = tuple(str(name).strip() for name in allowed_tools)
        if not normalized or any(not name for name in normalized):
            raise ValueError("allowed_tools must be a non-empty sequence of tool names")
        return normalized

    def _apply_tool_allowlist(self, tools):
        if self.allowed_tools is None:
            return tools
        legal_names = toolkit.legal_tool_names()
        unknown = [name for name in self.allowed_tools if name not in legal_names]
        if unknown:
            raise ValueError(f"unknown allowed tool: {', '.join(unknown)}")
        # 协作工具（黑板/信箱）是能力无关的内建工具，不受角色 allowed_tools 限制。
        collab_names = {"post_note", "read_notes", "send_message", "check_inbox"}
        allowed = set(self.allowed_tools) | collab_names
        return {
            name: tool
            for name, tool in tools.items()
            if name in allowed
        }

    def tool_signature(self):
        return tool_signature(self.tools)

    def build_prefix(self):
        return build_prompt_prefix(workspace=self.workspace, tools=self.tools)

    def _apply_prefix_state(self, prefix_state):
        self.prefix_state = prefix_state
        self.prefix = prefix_state.text

    def refresh_prefix(self, force=False):
        previous_hash = getattr(getattr(self, "prefix_state", None), "hash", None)
        previous_workspace_fingerprint = getattr(getattr(self, "prefix_state", None), "workspace_fingerprint", None)

        # 工作区事实相对稳定，所以这里按整体刷新；
        # 只有这些事实真的变化了，才重建完整 prefix。
        refreshed_workspace = WorkspaceContext.build(self.root)
        refreshed_workspace_fingerprint = refreshed_workspace.fingerprint()
        workspace_changed = force or refreshed_workspace_fingerprint != previous_workspace_fingerprint
        if workspace_changed:
            self.workspace = refreshed_workspace

        prefix_state = self.build_prefix() if workspace_changed or force or previous_hash is None else self.prefix_state
        prefix_changed = force or previous_hash != prefix_state.hash
        if prefix_changed:
            self._apply_prefix_state(prefix_state)

        self._last_prefix_refresh = {
            "workspace_changed": workspace_changed,
            "prefix_changed": prefix_changed,
        }
        return dict(self._last_prefix_refresh)

    def memory_text(self):
        return self.memory.render_memory_text()

    def history_text(self):
        history = self.session["history"]
        if not history:
            return "- empty"

        lines = []
        seen_reads = set()
        recent_start = max(0, len(history) - 6)
        for index, item in enumerate(history):
            recent = index >= recent_start
            if item["role"] == "tool" and item["name"] == "read_file" and not recent:
                path = str(item["args"].get("path", ""))
                if path in seen_reads:
                    continue
                seen_reads.add(path)

            if item["role"] == "tool":
                limit = 900 if recent else 180
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(clip(item["content"], limit))
            else:
                limit = 900 if recent else 220
                lines.append(f"[{item['role']}] {clip(item['content'], limit)}")

        return clip("\n".join(lines), MAX_HISTORY)

    def feature_enabled(self, name):
        return bool(self.feature_flags.get(str(name), False))

    def prompt(self, user_message):
        prompt, _ = self._build_prompt_and_metadata(user_message)
        return prompt

    def record(self, item):
        with self._state_lock:
            self.session["history"].append(item)
            self.session_path = self.session_store.save(self.session)

    @staticmethod
    def looks_sensitive_env_name(name):
        return securitylib.looks_sensitive_env_name(name)

    def is_secret_env_name(self, name):
        return securitylib.is_secret_env_name(name, secret_env_names=self.secret_env_names)

    def configured_secret_env_items(self):
        return securitylib.configured_secret_env_items(secret_env_names=self.secret_env_names)

    def detected_secret_env_items(self):
        return securitylib.detected_secret_env_items(secret_env_names=self.secret_env_names)

    def secret_env_summary(self):
        return securitylib.secret_env_summary(secret_env_names=self.secret_env_names)

    def detected_secret_env_summary(self):
        return securitylib.detected_secret_env_summary(secret_env_names=self.secret_env_names)

    def redact_text(self, text):
        return securitylib.redact_text(text, secret_env_names=self.secret_env_names)

    def redact_artifact(self, value, key=None):
        return securitylib.redact_artifact(value, key=key, secret_env_names=self.secret_env_names)

    def shell_env(self):
        return securitylib.shell_env(allowlist=self.shell_env_allowlist, root=self.root)

    def prompt_metadata(self, user_message, prompt):
        _, metadata = self._build_prompt_and_metadata(user_message)
        return metadata

    def _build_prompt_and_metadata(self, user_message):
        refresh = self.refresh_prefix()
        self.resume_state = self.evaluate_resume_state()
        prompt, metadata = self.context_manager.build(user_message)
        # 这里把“这轮 prompt 是怎么拼出来的”连同缓存相关状态一起记下来，
        # 后面 trace/report 才能解释清楚：为什么这一轮 prefix 变了、缓存有没有命中。
        metadata.update(
            {
                "prefix_chars": len(self.prefix),
                "workspace_chars": len(self.workspace.text()),
                "memory_chars": len(self.memory_text()),
                "history_chars": len(self.history_text()),
                "request_chars": len(user_message),
                "tool_count": len(self.tools),
                "workspace_docs": len(self.workspace.project_docs),
                "recent_commits": len(self.workspace.recent_commits),
                "prefix_hash": self.prefix_state.hash,
                "prompt_cache_key": self.prefix_state.hash,
                "workspace_fingerprint": self.prefix_state.workspace_fingerprint,
                "tool_signature": self.prefix_state.tool_signature,
                "workspace_changed": refresh["workspace_changed"],
                "prefix_changed": refresh["prefix_changed"],
                "prompt_cache_supported": bool(getattr(self.model_client, "supports_prompt_cache", False)),
                "resume_status": self.resume_state.get("status", CHECKPOINT_NONE_STATUS),
                "stale_summary_invalidations": int(self.resume_state.get("stale_summary_invalidations", 0)),
                "stale_paths": list(self.resume_state.get("stale_paths", [])),
                "runtime_identity_mismatch_fields": list(self.resume_state.get("runtime_identity_mismatch_fields", [])),
            }
        )
        metadata.update(self.detected_secret_env_summary())
        return prompt, metadata

    def emit_trace(self, task_state, event, payload=None):
        payload = self.redact_artifact(payload or {})
        payload["event"] = event
        payload["created_at"] = now()
        # trace 是运行中的逐事件时间线，适合回答“这一轮 agent 到底做了什么”。
        self.run_store.append_trace(task_state, payload)
        return payload

    def capture_workspace_snapshot(self):
        snapshot = {}
        for path in self.root.rglob("*"):
            try:
                relative_parts = path.relative_to(self.root).parts
            except ValueError:
                continue
            if any(part in IGNORED_PATH_NAMES for part in relative_parts):
                continue
            if not path.is_file():
                continue
            try:
                snapshot[path.relative_to(self.root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            except Exception:
                continue
        return snapshot

    @staticmethod
    def diff_workspace_snapshots(before, after):
        changed_paths = []
        summaries = []
        all_paths = sorted(set(before) | set(after))
        for path in all_paths:
            if before.get(path) == after.get(path):
                continue
            changed_paths.append(path)
            if path not in before:
                summaries.append(f"created:{path}")
            elif path not in after:
                summaries.append(f"deleted:{path}")
            else:
                summaries.append(f"modified:{path}")
        return changed_paths, summaries

    def create_checkpoint(self, task_state, user_message, trigger):
        return checkpointlib.create_checkpoint(self, task_state, user_message, trigger)

    def infer_next_step(self, task_state):
        return checkpointlib.infer_next_step(task_state)

    def update_memory_after_tool(self, name, args, result):
        """把少量高价值工具结果沉淀到 working memory。

        为什么存在：
        并不是每个工具结果都值得长期带进下一轮 prompt。完整结果已经进了
        `history`，这里只挑少量“下一轮大概率还会用到”的事实做提纯，
        例如最近读写过哪些文件、某个文件读出来的短摘要。

        输入 / 输出
        - 输入：工具名 `name`、参数 `args`、执行结果 `result`
        - 输出：无显式返回值，副作用是更新 `self.memory`

        在 agent 链路里的位置：
        它发生在 `run_tool()` 真正执行完工具之后、下一轮 prompt 组装之前。
        也就是说：工具结果先进入完整历史，再由这个函数择优沉淀成轻量记忆。
        """
        if not self.feature_enabled("memory"):
            return
        path = args.get("path")
        if not path:
            return

        with self._state_lock:
            canonical_path = self.memory.canonical_path(path)
            # 不是所有工具结果都进入工作记忆。
            # 读文件会生成摘要；写文件/patch 会让旧摘要失效，因为它们可能过期了。
            if name in {"read_file", "write_file", "patch_file"}:
                self.memory.remember_file(canonical_path)
            if name == "read_file":
                summary = memorylib.summarize_read_result(result)
                self.memory.set_file_summary(canonical_path, summary)
                self.memory.append_note(summary, tags=(canonical_path,), source=canonical_path)
            elif name in {"write_file", "patch_file"}:
                self.memory.invalidate_file_summary(canonical_path)

    def note_tool(self, name, args, result):
        self.update_memory_after_tool(name, args, result)

    def record_process_note_for_tool(self, name, metadata):
        status = str(metadata.get("tool_status", "")).strip()
        if status not in {"partial_success", "error", "rejected"}:
            return
        affected_paths = [str(path).strip() for path in metadata.get("affected_paths", []) if str(path).strip()]
        path_text = ", ".join(affected_paths) or "workspace"
        if status == "partial_success":
            text = f"{name} partial_success on {path_text}; inspect diff before retry"
        elif status == "error":
            text = f"{name} error on {path_text}; check the failure before retry"
        else:
            text = f"{name} rejected; choose a different action before retry"
        tags = ["process", status, *affected_paths]
        with self._state_lock:
            self.memory.append_note(text, tags=tuple(tags), source=name, kind="process")
            self.session["memory"] = self.memory.to_dict()

    def reject_durable_reason(self, note_text):
        text = str(note_text or "").strip()
        lowered = text.lower()
        if not text:
            return "empty"
        if REDACTED_VALUE in text or SECRET_SHAPED_TEXT_PATTERN.search(text):
            return "secret_shaped"
        checkpoint_like_prefixes = (
            "current goal",
            "current blocker",
            "next step",
            "current phase",
            "key files",
            "freshness",
            "当前目标",
            "当前卡点",
            "下一步",
            "当前阶段",
            "关键文件",
            "已完成",
            "已排除",
        )
        if any(lowered.startswith(prefix) for prefix in checkpoint_like_prefixes):
            return "transient_task_state"
        if re.search(r"(?i)\b(stdout|stderr|traceback|exit_code)\b", text) or len(text) > 220:
            return "noisy_output"
        return ""

    def extract_durable_promotions(self, user_message, final_answer):
        user_text = str(user_message or "")
        if not (DURABLE_MEMORY_INTENT_PATTERN.search(user_text) or DURABLE_MEMORY_INTENT_ZH_PATTERN.search(user_text)):
            return [], []
        promotions = []
        rejections = []
        for line in str(final_answer or "").splitlines():
            text = line.strip()
            if not text or REDACTED_VALUE in text:
                continue
            for topic, pattern in DURABLE_MEMORY_LINE_PATTERNS:
                match = pattern.match(text)
                if not match:
                    continue
                note_text = match.group(1).strip()
                if note_text:
                    reason = self.reject_durable_reason(note_text)
                    if reason:
                        rejections.append(f"{topic}:{reason}")
                        break
                    promotions.append((topic, note_text))
                break
        return promotions, rejections

    def promote_durable_memory(self, user_message, final_answer):
        # 子 agent（durable_memory_writes=False）不写持久记忆，只能通过 Result.proposed_memory 提建议。
        if not self.durable_memory_writes:
            self.last_durable_promotions = []
            self.last_durable_rejections = []
            self.last_durable_superseded = []
            return [], [], []
        with self._state_lock:
            promotions, rejections = self.extract_durable_promotions(user_message, final_answer)
            promoted, superseded = self.memory.promote_durable(promotions)
            self.session["memory"] = self.memory.to_dict()
            self.last_durable_promotions = promoted
            self.last_durable_rejections = rejections
            self.last_durable_superseded = superseded
            return promoted, rejections, superseded

    def ask(self, user_message):
        from .agent_loop import AgentLoop

        return AgentLoop(self).run(user_message)

    def execute_tool(self, name, args):
        result = self.tool_executor.execute(name, args)
        self._last_tool_result_metadata = dict(result.metadata)
        return result

    def run_tool(self, name, args):
        """执行一次工具调用，并在执行前后套上完整护栏。

        为什么存在：
        在 agent 系统里，真正危险的不是“模型会不会想调用工具”，而是
        “平台有没有在执行前把边界守住”。这个函数就是工具层的总闸口：
        所有工具调用都必须先经过它，不能让模型直接碰到底层函数。

        输入 / 输出：
        - 输入：工具名 `name`，参数字典 `args`
        - 输出：字符串结果。无论是成功结果还是错误信息，都会统一返回文本，
          这样模型下一轮都能继续消费这份反馈。

        在 agent 链路里的位置：
        它位于 `ask()` 的“模型决定要调用工具”之后，是控制循环里真正把模型
        意图落到外部世界的一步。因此这里串起了几乎所有安全与可控设计：
        工具是否存在、参数是否合法、是否重复、是否需要审批、执行结果是否裁剪、
        是否需要回写记忆。
        """
        return self.execute_tool(name, args).content

    def repeated_tool_call(self, name, args):
        # agent 很常见的一种坏循环，是在没有新信息的情况下反复发起同一调用。
        # 这里提前挡掉最简单的这种循环。
        tool_events = [item for item in self.session["history"] if item["role"] == "tool"]
        if len(tool_events) < 2:
            return False
        recent = tool_events[-2:]
        return all(item["name"] == name and item["args"] == args for item in recent)

    @staticmethod
    def new_task_id():
        return "task_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    @staticmethod
    def new_run_id():
        return "run_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    def build_report(self, task_state):
        # report 是一次运行的最终摘要；
        # 和 trace 的区别在于，trace 关注过程，report 关注结果与关键指标。
        return {
            "run_id": task_state.run_id,
            "task_id": task_state.task_id,
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "final_answer": task_state.final_answer,
            "tool_steps": task_state.tool_steps,
            "attempts": task_state.attempts,
            "checkpoint_id": task_state.checkpoint_id,
            "resume_status": task_state.resume_status,
            "parent_run_id": self.parent_run_id,
            "role": self.role,
            "task_state": task_state.to_dict(),
            "prompt_metadata": self.last_prompt_metadata,
            "durable_promotions": list(self.last_durable_promotions),
            "durable_rejections": list(self.last_durable_rejections),
            "durable_superseded": list(self.last_durable_superseded),
            "redacted_env": self.detected_secret_env_summary(),
            "multi_agent": self._orchestrator.summary() if self._orchestrator is not None else None,
        }

    def tool_example(self, name):
        return toolkit.tool_example(name)

    def validate_tool(self, name, args):
        """把通用工具校验和 runtime 级额外约束串起来。"""
        toolkit.validate_tool(self.tool_context(), name, args)

    def tool_context(self):
        return ToolContext(
            root=self.root,
            path_resolver=self.path,
            shell_env_provider=self.shell_env,
            depth=self.depth,
            max_depth=self.max_depth,
            spawn_delegate=self.spawn_delegate,
        )

    def spawn_delegate(self, args):
        task = str(args.get("task", "")).strip()
        # 子 agent 新建独立 client（有工厂时），避免与父共用实例串扰 usage/剧本。
        child_client = self.model_client_factory() if self.model_client_factory else self.model_client
        parent_run_id = self.current_task_state.run_id if getattr(self, "current_task_state", None) else ""
        child = Pico(
            model_client=child_client,
            workspace=self.workspace,
            session_store=self.session_store.child_store(),
            run_store=self.run_store,
            approval_policy="never",
            max_steps=int(args.get("max_steps", 3)),
            max_new_tokens=self.max_new_tokens,
            depth=self.depth + 1,
            max_depth=self.max_depth,
            read_only=True,
            secret_env_names=self.secret_env_names,
            shell_env_allowlist=self.shell_env_allowlist,
            context_profile=self.context_profile,
            model_client_factory=self.model_client_factory,
            durable_memory_writes=False,
            parent_run_id=parent_run_id,
            role="delegate",
        )
        # 委派的目标是“调查”，不是“放权执行”。
        # 子 agent 以只读方式运行、步数更少，最后只把结论文本返回给父 agent。
        child.session["parent_session_id"] = self.session["id"]
        child.session["memory"]["task"] = task
        child.session["memory"]["notes"] = [clip(self.history_text(), 300)]
        child.session_store.save(child.session)
        return "delegate_result:\n" + child.ask(task)

    def tool_list_files(self, args):
        return toolkit.tool_list_files(self.tool_context(), args)

    def tool_read_file(self, args):
        return toolkit.tool_read_file(self.tool_context(), args)

    def tool_search(self, args):
        return toolkit.tool_search(self.tool_context(), args)

    def tool_run_shell(self, args):
        return toolkit.tool_run_shell(self.tool_context(), args)

    def tool_write_file(self, args):
        return toolkit.tool_write_file(self.tool_context(), args)

    def tool_patch_file(self, args):
        return toolkit.tool_patch_file(self.tool_context(), args)

    def tool_delegate(self, args):
        return toolkit.tool_delegate(self.tool_context(), args)

    def approve(self, name, args):
        if self.read_only:
            return False
        if self.approval_policy == "auto":
            return True
        if self.approval_policy == "never":
            return False
        # 并行场景下，多个子 agent 的审批请求经 broker 串行化并标注来源。
        if self.approval_broker is not None:
            source = f"{self.role or 'agent'}"
            return bool(self.approval_broker.request(source, name, args))
        try:
            answer = input(f"approve {name} {json.dumps(args, ensure_ascii=True)}? [y/N] ")
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}

    @staticmethod
    def parse(raw):
        """把模型原始输出解析成 runtime 可执行的动作或最终答案。

        为什么存在：
        模型输出首先是自然语言文本，而 runtime 需要的是结构化决策：
        “这是工具调用”还是“这是最终答案”。如果没有这层解析，后面的工具校验、
        审批和执行链路就没法可靠工作。

        输入 / 输出：
        - 输入：模型返回的原始文本 `raw`
        - 输出：`(kind, payload)`，其中 `kind` 可能是 `tool`、`final`、`retry`

        在 agent 链路里的位置：
        它位于 `model_client.complete()` 之后、`run_tool()` 之前，是模型输出
        进入平台控制流的第一道结构化关口。
        """
        raw = str(raw)
        # 这里支持三种工具格式：
        # 1. <tools>...</tools> 里包 {"calls": [...]}，适合并行只读调用
        # 2. <tool>...</tool> 里包 JSON，适合简短单次调用
        # 3. XML 风格属性/子标签，适合写文件这类多行内容
        if "<tools>" in raw and ("<final>" not in raw or raw.find("<tools>") < raw.find("<final>")):
            body = Pico.extract(raw, "tools")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return "retry", Pico.retry_notice("<tools> body must be valid JSON")
            if not isinstance(payload, dict) or "calls" not in payload:
                return "retry", Pico.retry_notice('<tools> must contain {"calls": [...]}')
            calls = payload["calls"]
            if not isinstance(calls, list) or not calls:
                return "retry", Pico.retry_notice("calls must be a non-empty array")
            for i, call in enumerate(calls):
                if not isinstance(call, dict):
                    return "retry", Pico.retry_notice(f"call at index {i} must be a JSON object")
                if not str(call.get("name", "")).strip():
                    return "retry", Pico.retry_notice(f"call at index {i} is missing a tool name")
                args = call.get("args", {})
                if args is None:
                    call["args"] = {}
                elif not isinstance(args, dict):
                    return "retry", Pico.retry_notice(f"call at index {i} has invalid args")
            return "tools", {"calls": calls}
        if "<tool>" in raw and ("<final>" not in raw or raw.find("<tool>") < raw.find("<final>")):
            body = Pico.extract(raw, "tool")
            try:
                payload = json.loads(body)
            except Exception:
                return "retry", Pico.retry_notice("model returned malformed tool JSON")
            if not isinstance(payload, dict):
                return "retry", Pico.retry_notice("tool payload must be a JSON object")
            if not str(payload.get("name", "")).strip():
                return "retry", Pico.retry_notice("tool payload is missing a tool name")
            args = payload.get("args", {})
            if args is None:
                payload["args"] = {}
            elif not isinstance(args, dict):
                return "retry", Pico.retry_notice()
            return "tool", payload
        if "<tool" in raw and ("<final>" not in raw or raw.find("<tool") < raw.find("<final>")):
            payload = Pico.parse_xml_tool(raw)
            if payload is not None:
                return "tool", payload
            return "retry", Pico.retry_notice()
        if "<final>" in raw:
            final = Pico.extract(raw, "final").strip()
            if final:
                return "final", final
            return "retry", Pico.retry_notice("model returned an empty <final> answer")
        raw = raw.strip()
        if raw:
            return "final", raw
        return "retry", Pico.retry_notice("model returned an empty response")

    @staticmethod
    def retry_notice(problem=None):
        prefix = "Runtime notice"
        if problem:
            prefix += f": {problem}"
        else:
            prefix += ": model returned malformed tool output"
        return (
            f"{prefix}. Reply with a valid <tool> call or a non-empty <final> answer. "
            'For multi-line files, prefer <tool name="write_file" path="file.py"><content>...</content></tool>.'
        )

    @staticmethod
    def parse_xml_tool(raw):
        match = re.search(r"<tool(?P<attrs>[^>]*)>(?P<body>.*?)</tool>", raw, re.S)
        if not match:
            return None
        attrs = Pico.parse_attrs(match.group("attrs"))
        name = str(attrs.pop("name", "")).strip()
        if not name:
            return None

        body = match.group("body")
        args = dict(attrs)
        for key in ("content", "old_text", "new_text", "command", "task", "pattern", "path"):
            if f"<{key}>" in body:
                args[key] = Pico.extract_raw(body, key)

        body_text = body.strip("\n")
        if name == "write_file" and "content" not in args and body_text:
            args["content"] = body_text
        if name == "delegate" and "task" not in args and body_text:
            args["task"] = body_text.strip()
        return {"name": name, "args": args}

    @staticmethod
    def parse_attrs(text):
        attrs = {}
        for match in re.finditer(r"""([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""", text):
            attrs[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
        return attrs

    @staticmethod
    def extract(text, tag):
        start_tag = f"<{tag}>"
        end_tag = f"</{tag}>"
        start = text.find(start_tag)
        if start == -1:
            return text
        start += len(start_tag)
        end = text.find(end_tag, start)
        if end == -1:
            return text[start:].strip()
        return text[start:end].strip()

    @staticmethod
    def extract_raw(text, tag):
        start_tag = f"<{tag}>"
        end_tag = f"</{tag}>"
        start = text.find(start_tag)
        if start == -1:
            return text
        start += len(start_tag)
        end = text.find(end_tag, start)
        if end == -1:
            return text[start:]
        return text[start:end]

    def reset(self):
        self.session["history"] = []
        self.session["memory"].clear()
        self.session["memory"].update(memorylib.default_memory_state())
        self.memory = memorylib.LayeredMemory(self.session["memory"], workspace_root=self.root)
        self.session_store.save(self.session)

    def path(self, raw_path):
        path = Path(raw_path)
        path = path if path.is_absolute() else self.root / path
        resolved = path.resolve()
        # 所有文件类工具都被锚定在 workspace root 之下。
        # 这样既能防住 "../" 逃逸，也能防住符号链接解析后跳出仓库。
        if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
            raise ValueError(f"path escapes workspace: {raw_path}")
        return resolved
