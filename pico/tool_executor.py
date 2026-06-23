"""Structured tool execution for the agent runtime."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import re

from .workspace import clip


@dataclass(frozen=True)
class ToolExecutionResult:
    content: str
    metadata: dict


def _metadata(
    tool_status,
    tool_error_code="",
    security_event_type="",
    risk_level="low",
    read_only=True,
    affected_paths=None,
    workspace_changed=False,
    workspace_fingerprint="",
    diff_summary=None,
):
    result = {
        "tool_status": tool_status,
        "tool_error_code": tool_error_code,
        "security_event_type": security_event_type,
        "risk_level": risk_level,
        "read_only": read_only,
        "affected_paths": list(affected_paths or []),
        "workspace_changed": bool(workspace_changed),
        "diff_summary": list(diff_summary or []),
    }
    if workspace_fingerprint:
        result["workspace_fingerprint"] = workspace_fingerprint
    return result


READ_ONLY_TOOL_NAMES = {"read_file", "search", "list_files"}
MAX_READ_WORKERS = 4
MAX_READ_BATCH_SIZE = 8


class ToolExecutor:
    def __init__(self, agent):
        self.agent = agent

    def execute_batch(self, calls):
        """分批执行工具调用：只读并行，写操作串行。

        执行模型：
        阶段 1 —— 所有只读工具（read_file / search / list_files）并行执行，
                 利用 ThreadPoolExecutor 减少 IO 等待。
        阶段 2 —— 非只读工具在阶段 1 全部完成后，按原始顺序逐个串行执行，
                 每个都走完整的 validate → approve → execute → snapshot 链路。

        为什么这样设计：
        - 读操作互不依赖，并行化收益最大
        - 写操作之间有潜在的副作用依赖（两个写同一文件），串行安全
        - 写操作可能依赖读操作的结果（先读配置再改），所以写必须在读之后

        输入 / 输出：
        - 输入：`calls`，每个元素是 {"name": ..., "args": {...}}
        - 输出：拼接后的文本结果，按原始顺序排列，每条以 [tool:...] 标记
        """
        agent = self.agent

        if not calls or not isinstance(calls, list):
            return "error: tools batch requires a non-empty calls array"

        if len(calls) > MAX_READ_BATCH_SIZE:
            return f"error: too many parallel calls ({len(calls)}), max is {MAX_READ_BATCH_SIZE}"

        # 1. 按原始索引分离读/写，预校验所有调用
        read_calls = []   # (original_index, name, args)
        write_calls = []  # (original_index, name, args)

        for i, call in enumerate(calls):
            name = str(call.get("name", "")).strip()
            args = call.get("args", {}) or {}
            try:
                agent.validate_tool(name, args)
            except Exception as exc:
                return f"error: invalid arguments for {name} at index {i}: {exc}"
            if name in READ_ONLY_TOOL_NAMES:
                read_calls.append((i, name, args))
            else:
                write_calls.append((i, name, args))

        results = [None] * len(calls)

        # ==== 阶段 2：并行执行只读调用 ====
        if read_calls:
            def _run_read(index, name, args):
                try:
                    result = agent.execute_tool(name, args)
                    agent.update_memory_after_tool(name, args, result.content)
                    agent.record_process_note_for_tool(name, dict(result.metadata))
                    return index, result.content, dict(result.metadata)
                except Exception as exc:
                    return index, f"error: tool {name} failed: {exc}", {
                        "tool_status": "error",
                        "tool_error_code": "tool_failed",
                        "risk_level": "low",
                        "read_only": True,
                        "affected_paths": [],
                        "workspace_changed": False,
                        "diff_summary": [],
                    }

            with ThreadPoolExecutor(max_workers=min(MAX_READ_WORKERS, len(read_calls))) as pool:
                futures = [
                    pool.submit(_run_read, idx, name, args)
                    for idx, name, args in read_calls
                ]
                for future in as_completed(futures):
                    index, content, metadata = future.result()
                    results[index] = (content, metadata)

        # ==== 阶段 3：串行执行非只读调用 ====
        for idx, name, args in write_calls:
            try:
                result = agent.execute_tool(name, args)
                agent.update_memory_after_tool(name, args, result.content)
                agent.record_process_note_for_tool(name, dict(result.metadata))
                results[idx] = (result.content, dict(result.metadata))
            except Exception as exc:
                results[idx] = (f"error: tool {name} failed: {exc}", {
                    "tool_status": "error",
                    "tool_error_code": "tool_failed",
                })

        # 4. 按原始顺序拼接结果
        parts = []
        for i, call in enumerate(calls):
            name = str(call.get("name", "")).strip()
            args = call.get("args", {}) or {}
            content, _metadata = results[i]
            parts.append(f"[tool:{name} args={args}]\n{content}")

        return "\n\n".join(parts)

    def execute(self, name, args):
        agent = self.agent

        #  1. 工具白名单校验

        if agent.allowed_tools is not None and name not in agent.allowed_tools:
            return ToolExecutionResult(
                content=f"error: tool '{name}' is not allowed in this run",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="tool_not_allowed",
                    risk_level="high",
                    read_only=False,
                ),
            )

        tool = agent.tools.get(name)

         # ---------- 2. 工具是否存在 ----------
        if tool is None:
            return ToolExecutionResult(
                content=f"error: unknown tool '{name}'",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="unknown_tool",
                    risk_level="high",
                    read_only=False,
                ),
            )
        # ---------- 3. 参数合法性校验 ----------
        try:
            agent.validate_tool(name, args)
        except Exception as exc:
            example = agent.tool_example(name)
            message = f"error: invalid arguments for {name}: {exc}"
            if example:
                message += f"\nexample: {example}"
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            return ToolExecutionResult(
                content=message,
                metadata=_metadata(
                    "rejected",
                    tool_error_code="invalid_arguments",
                    security_event_type=security_event_type,
                    risk_level="high" if tool["risky"] else "low",
                    read_only=not tool["risky"],
                ),
            )
         # ---------- 4. 防止重复调用 ----------
        if agent.repeated_tool_call(name, args):
            return ToolExecutionResult(
                content=f"error: repeated identical tool call for {name}; choose a different tool or return a final answer",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="repeated_identical_call",
                    risk_level="high" if tool["risky"] else "low",
                    read_only=not tool["risky"],
                ),
            )
         # ---------- 5. 高风险工具审批 ----------
        if tool["risky"] and not agent.approve(name, args):
            return ToolExecutionResult(
                content=f"error: approval denied for {name}",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="approval_denied",
                    security_event_type="read_only_block" if agent.read_only else "approval_denied",
                    risk_level="high",
                    read_only=False,
                ),
            )
        # ---------- 6. 执行前：工作区快照 ----------

        before_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else {}
        after_snapshot = before_snapshot
        try:
            # ---------- 7. 真正执行工具 ----------
            content = clip(tool["run"](args))
            # 执行后再次快照（仅高风险工具）
            after_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else before_snapshot
            # 对比前后快照，计算影响路径与 diff
            affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            tool_status = "ok"
            tool_error_code = ""

             # ---------- 8. shell 工具的特殊处理 ----------
            if name == "run_shell":
                match = re.search(r"exit_code:\s*(-?\d+)", content)
                exit_code = int(match.group(1)) if match else 0
                if exit_code != 0 and workspace_changed:
                    tool_status = "partial_success"
                    tool_error_code = "tool_partial_success"
                elif exit_code != 0:
                    tool_status = "error"
                    tool_error_code = "tool_failed"
            # ---------- 9. 更新 Agent 记忆与审计 ----------
            agent.update_memory_after_tool(name, args, content)
            metadata = _metadata(
                tool_status,
                tool_error_code=tool_error_code,
                risk_level="high" if tool["risky"] else "low",
                read_only=not tool["risky"],
                affected_paths=affected_paths,
                workspace_changed=workspace_changed,
                workspace_fingerprint=agent.workspace.fingerprint(),
                diff_summary=diff_summary,
            )
            agent.record_process_note_for_tool(name, metadata)
            return ToolExecutionResult(content=content, metadata=metadata)
        except Exception as exc:
            after_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else before_snapshot
            affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            metadata = _metadata(
                "partial_success" if workspace_changed else "error",
                tool_error_code="tool_partial_success" if workspace_changed else "tool_failed",
                security_event_type=security_event_type,
                risk_level="high" if tool["risky"] else "low",
                read_only=not tool["risky"],
                affected_paths=affected_paths,
                workspace_changed=workspace_changed,
                workspace_fingerprint=agent.workspace.fingerprint(),
                diff_summary=diff_summary,
            )
            agent.record_process_note_for_tool(name, metadata)
            return ToolExecutionResult(content=f"error: tool {name} failed: {exc}", metadata=metadata)
