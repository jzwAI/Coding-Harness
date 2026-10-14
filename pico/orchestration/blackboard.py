"""Blackboard：子 agent 之间共享信息的只追加公告墙 + 定向信箱（线程安全）。

两种非阻塞通信：
- 黑板（广播）：谁有确定结论就 post_note 贴一条（带证据），别人 read_notes 自己读。
- 信箱（定向）：send_message 给某个任务留言，投完即返回；收件人 check_inbox 主动拉取。

两者都不会阻塞调用方，因此并行子 agent 之间不会互等死锁。黑板只追加，便于审计；
每次写入都加锁并原子落盘到 .pico/runs/<run_id>/blackboard.json。

每个子 agent 拿到的是一个绑定了自身 task_id 的 CollabHandle，模型不需要（也无法伪造）
"from" 身份——由 runtime 固定。
"""

from __future__ import annotations

import json
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

NOTE_KINDS = {"finding", "decision", "interface", "warning", "note"}
DEFAULT_READ_LIMIT = 20
MAX_TEXT = 2000


def _now():
    return datetime.now(timezone.utc).isoformat()


class Blackboard:
    def __init__(self, store_path=None):
        self._lock = threading.RLock()
        self._notes = []            # 只追加的条目列表
        self._inboxes = {}          # task_id -> [message, ...]
        self._seq = 0
        self._path = Path(store_path) if store_path else None

    # ---------- 黑板（广播） ----------

    def post(self, from_task, key, text, evidence="", kind="note"):
        with self._lock:
            self._seq += 1
            note = {
                "id": f"n{self._seq}",
                "from_task": str(from_task or ""),
                "kind": kind if kind in NOTE_KINDS else "note",
                "key": str(key or "").strip(),
                "text": str(text or "").strip()[:MAX_TEXT],
                "evidence": str(evidence or "").strip()[:300],
                "created_at": _now(),
            }
            self._notes.append(note)
            self._persist()
            return note["id"]

    def read(self, key_prefix="", limit=DEFAULT_READ_LIMIT):
        key_prefix = str(key_prefix or "")
        with self._lock:
            matches = [n for n in self._notes if n["key"].startswith(key_prefix)] if key_prefix else list(self._notes)
            return matches[-int(limit):]

    # ---------- 信箱（定向） ----------

    def send(self, from_task, to_task, text):
        with self._lock:
            self._seq += 1
            msg = {
                "id": f"m{self._seq}",
                "from_task": str(from_task or ""),
                "text": str(text or "").strip()[:MAX_TEXT],
                "created_at": _now(),
                "read": False,
            }
            self._inboxes.setdefault(str(to_task), []).append(msg)
            self._persist()
            return msg["id"]

    def inbox(self, task_id, unread_only=True, mark_read=True):
        with self._lock:
            msgs = self._inboxes.get(str(task_id), [])
            out = [dict(m) for m in msgs if (m["read"] is False or not unread_only)]
            if mark_read:
                for m in msgs:
                    m["read"] = True
                if out:
                    self._persist()
            return out

    # ---------- 落盘 / 快照 ----------

    def to_dict(self):
        with self._lock:
            return {"notes": list(self._notes), "inboxes": {k: list(v) for k, v in self._inboxes.items()}}

    def _persist(self):
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False,
            dir=str(self._path.parent), prefix=self._path.name + ".", suffix=".tmp",
        ) as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            temp_name = handle.name
        Path(temp_name).replace(self._path)


class CollabHandle:
    """注入给单个子 agent 的协作句柄：from 身份固定为该 agent 的 task_id。"""

    def __init__(self, blackboard, task_id):
        self._bb = blackboard
        self._task_id = str(task_id)

    def post_note(self, key, text, evidence="", kind="note"):
        note_id = self._bb.post(self._task_id, key, text, evidence=evidence, kind=kind)
        return f"posted {note_id} to blackboard (key={key})"

    def read_notes(self, key_prefix="", limit=DEFAULT_READ_LIMIT):
        notes = self._bb.read(key_prefix=key_prefix, limit=limit)
        if not notes:
            return "(no matching notes on the blackboard)"
        lines = []
        for n in notes:
            ev = f" [{n['evidence']}]" if n["evidence"] else ""
            lines.append(f"- {n['key']} (from {n['from_task']}): {n['text']}{ev}")
        return "\n".join(lines)

    def send_message(self, to_task, text):
        msg_id = self._bb.send(self._task_id, to_task, text)
        return f"sent {msg_id} to {to_task}"

    def check_inbox(self):
        msgs = self._bb.inbox(self._task_id)
        if not msgs:
            return "(inbox empty)"
        return "\n".join(f"- from {m['from_task']}: {m['text']}" for m in msgs)
