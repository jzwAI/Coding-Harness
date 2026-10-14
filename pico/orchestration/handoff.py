"""上下文交接：Brief（父→子）与 Result（子→父）。

Brief：由 build_brief 根据任务卡生成一段文本，作为子 agent 的输入，
       取代旧实现"把父历史截 300 字符"的粗糙做法。
Result：子 agent 的 <final> 必须是结构化 JSON；parse_result 负责解析、
        校验、失败降级，并由 runtime 用真实数据覆盖模型自述的字段。
"""

from __future__ import annotations

import json

RESULT_STATUSES = {"done", "blocked", "failed"}

RESULT_SCHEMA_HINT = (
    'Reply ONLY with a JSON object as your <final>, shaped like:\n'
    '{"status":"done|blocked|failed","summary":"...","findings":[{"claim":"...","evidence":"path:line"}],'
    '"changed_files":["..."],"tests":[{"cmd":"...","exit_code":0}],'
    '"open_questions":["..."],"proposed_memory":["Decision: ..."],"confidence":0.8}'
)


def build_brief(task, *, dep_results=None, file_summaries=None, retry_error="", max_chars=4000):
    """根据任务卡生成 Brief 文本。

    - task：task_board 的任务卡
    - dep_results：{dep_id: Result dict}，依赖任务的结果摘要
    - file_summaries：{path: summary_str}，来自父 memory，已做过新鲜度过滤
    - retry_error：非空表示这是重试，把上次失败原因写进去
    - max_chars：Brief 总长度上限（调用方按子 agent 预算的 ~15% 传入）
    """
    dep_results = dep_results or {}
    file_summaries = file_summaries or {}
    lines = []

    lines.append(f"# Task {task['task_id']} (role: {task['role']})")
    lines.append(f"\n## Goal\n{task['goal']}")

    if task.get("acceptance"):
        lines.append("\n## Acceptance criteria")
        lines.extend(f"- {item}" for item in task["acceptance"])

    if task.get("verify"):
        lines.append("\n## Verify commands (will be run after you finish)")
        lines.extend(f"- {item}" for item in task["verify"])

    # 约束：写范围
    scope = task.get("write_scope") or []
    if task["role"] in {"explorer", "reviewer", "planner"} or not scope:
        lines.append("\n## Constraints\n- Do NOT modify any files. This is a read-only task." if not scope else "")
    if scope:
        lines.append("\n## Write scope (you may ONLY modify these paths)")
        lines.extend(f"- {item}" for item in scope)
        lines.append("Modifying anything outside this list will be rejected as a scope violation.")

    facts = (task.get("inputs") or {}).get("facts") or []
    if facts:
        lines.append("\n## Established facts (already confirmed, do not re-verify)")
        lines.extend(f"- {item}" for item in facts)

    hint_files = (task.get("inputs") or {}).get("files") or []
    if hint_files:
        lines.append("\n## Suggested files to look at first")
        for path in hint_files:
            summary = file_summaries.get(path)
            lines.append(f"- {path}" + (f" — {summary}" if summary else ""))

    if dep_results:
        lines.append("\n## Results from prerequisite tasks")
        for dep_id, result in dep_results.items():
            summary = (result or {}).get("summary", "") if isinstance(result, dict) else str(result)
            lines.append(f"- [{dep_id}] {summary}".strip())

    if retry_error:
        lines.append("\n## Previous attempt failed — fix this")
        lines.append(str(retry_error))

    lines.append("\n## How to finish\n" + RESULT_SCHEMA_HINT)

    text = "\n".join(line for line in lines if line is not None)
    if len(text) > max_chars:
        text = text[: max_chars - 3] + "..."
    return text


def _coerce_result(data):
    status = str(data.get("status", "")).strip().lower()
    if status not in RESULT_STATUSES:
        status = "done" if data.get("summary") else "failed"
    tests = []
    for item in data.get("tests", []) or []:
        if isinstance(item, dict):
            tests.append({"cmd": str(item.get("cmd", "")).strip(), "exit_code": item.get("exit_code")})
    findings = []
    for item in data.get("findings", []) or []:
        if isinstance(item, dict):
            findings.append({"claim": str(item.get("claim", "")).strip(), "evidence": str(item.get("evidence", "")).strip()})
    confidence = data.get("confidence")
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = None
    needs = []
    for item in data.get("needs", []) or []:
        if isinstance(item, dict) and str(item.get("question", "")).strip():
            needs.append({"question": str(item.get("question", "")).strip(),
                          "suggested_source": str(item.get("suggested_source", "")).strip()})
        elif isinstance(item, str) and item.strip():
            needs.append({"question": item.strip(), "suggested_source": ""})
    return {
        "status": status,
        "summary": str(data.get("summary", "")).strip(),
        "findings": findings,
        "changed_files": [str(p).strip() for p in (data.get("changed_files") or []) if str(p).strip()],
        "tests": tests,
        "open_questions": [str(q).strip() for q in (data.get("open_questions") or []) if str(q).strip()],
        "proposed_memory": [str(m).strip() for m in (data.get("proposed_memory") or []) if str(m).strip()],
        "needs": needs,
        "confidence": confidence,
        "parse_ok": True,
    }


def _extract_json_object(text):
    """从 final 文本里抽出第一个平衡的 JSON 对象（容忍模型多说了几句话）。"""
    text = str(text)
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    return None


def parse_result(final_text):
    """把子 agent 的 <final> 解析成结构化 Result。

    返回 (result_dict, ok)：
    - 能解析：ok=True，result_dict["parse_ok"]=True
    - 不能解析：ok=False，降级为 {status: failed, summary: 截断原文, parse_ok: False}
    """
    raw = _extract_json_object(final_text)
    if raw is not None:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return _coerce_result(data), True
        except (json.JSONDecodeError, ValueError):
            pass
    return {
        "status": "failed",
        "summary": str(final_text or "").strip()[:600],
        "findings": [],
        "changed_files": [],
        "tests": [],
        "open_questions": [],
        "proposed_memory": [],
        "needs": [],
        "confidence": None,
        "parse_ok": False,
    }, False
