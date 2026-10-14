"""Git worktree 管理：给每个可写 worker 一个隔离的工作区。

每个 worker 在 .pico/worktrees/<task> 这个独立文件夹、独立分支上改代码，
互不干扰，也不碰主工作区。改动最后由 Integrator 合并、验证通过后才应用回主工作区。

非 git 仓库时不可用（is_git_repo() 为 False），orchestrator 会退回串行、
直接写主工作区的 Phase 1 行为。
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _run(args, cwd, timeout=30):
    """跑一条 git 命令，返回 (returncode, stdout, stderr)。"""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False, timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


class WorktreeManager:
    def __init__(self, root):
        self.root = Path(root)
        self.worktrees_dir = self.root / ".pico" / "worktrees"

    def is_git_repo(self):
        code, out, _ = _run(["rev-parse", "--is-inside-work-tree"], self.root)
        return code == 0 and out == "true"

    def head_commit(self):
        code, out, _ = _run(["rev-parse", "HEAD"], self.root)
        return out if code == 0 else ""

    def has_uncommitted_changes(self):
        code, out, _ = _run(["status", "--porcelain"], self.root)
        return bool(out) if code == 0 else False

    def add_worktree(self, task_id, base_commit, branch):
        """新建 worktree + 分支。返回 (ok, path, message)。"""
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        path = self.worktrees_dir / task_id
        if path.exists():
            self.remove_worktree(task_id)   # 清理上一次的残留
        start = base_commit or self.head_commit() or "HEAD"
        code, _out, err = _run(["worktree", "add", "--force", "-b", branch, str(path), start], self.root)
        if code != 0:
            # 分支可能已存在，换成不建分支、直接签出已有分支
            code2, _o2, err2 = _run(["worktree", "add", "--force", str(path), branch], self.root)
            if code2 != 0:
                return False, "", f"worktree add failed: {err or err2}"
        return True, str(path), ""

    def commit_all(self, task_id, message):
        """把 worktree 里的所有改动提交成一个 commit。返回 (committed, message)。"""
        path = self.worktrees_dir / task_id
        _run(["add", "-A"], path)
        # 没有改动时 commit 会失败，用 diff --cached 判断是否有东西要提交
        code, out, _ = _run(["diff", "--cached", "--name-only"], path)
        if code != 0 or not out:
            return False, "no changes to commit"
        code, _o, err = _run(
            ["-c", "user.email=pico@local", "-c", "user.name=pico", "commit", "-m", message, "--no-verify"],
            path,
        )
        return (code == 0), ("" if code == 0 else err)

    def changed_files(self, task_id, base_commit):
        """worker 实际改动的文件：相对 base_commit 的修改/删除 + 新建未跟踪文件。"""
        path = self.worktrees_dir / task_id
        files = set()
        code, out, _ = _run(["diff", "--name-only", base_commit], path)
        if code == 0 and out:
            files.update(line.strip() for line in out.splitlines() if line.strip())
        code, out, _ = _run(["ls-files", "--others", "--exclude-standard"], path)
        if code == 0 and out:
            files.update(line.strip() for line in out.splitlines() if line.strip())
        # 过滤自动生成目录
        ignored = {".pico", "__pycache__", ".pytest_cache", ".ruff_cache"}
        return sorted(f for f in files if not any(part in ignored for part in Path(f).parts))

    def remove_worktree(self, task_id):
        path = self.worktrees_dir / task_id
        _run(["worktree", "remove", "--force", str(path)], self.root)
        if path.exists():
            import shutil
            shutil.rmtree(path, ignore_errors=True)

    def prune(self):
        _run(["worktree", "prune"], self.root)
