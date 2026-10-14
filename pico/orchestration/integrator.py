"""Integrator：把各 worker 分支合并到集成工作区，验证后再应用回主工作区。

流程：
1. integrate(task_ids)：在 .pico/worktrees/_integration（基于 base_commit）里，
   按依赖顺序逐个 git merge --no-ff 各 worker 分支；冲突则 abort 并报告。
2. apply_integration()：把集成分支的改动应用回主工作区（高风险，需审批）。

非 git 仓库时不可用，orchestrator 会退回 Phase 1 的直接写主工作区行为。
"""

from __future__ import annotations

from pathlib import Path

from .worktree import _run


class Integrator:
    INTEGRATION_TASK = "_integration"

    def __init__(self, root, base_commit, run_id):
        self.root = Path(root)
        self.base_commit = base_commit
        self.run_id = run_id
        self.worktrees_dir = self.root / ".pico" / "worktrees"
        self.path = self.worktrees_dir / self.INTEGRATION_TASK
        self.branch = f"pico/{run_id}/integration"

    def _ensure_integration_worktree(self):
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            return True, ""
        start = self.base_commit or "HEAD"
        code, _o, err = _run(["worktree", "add", "--force", "-b", self.branch, str(self.path), start], self.root)
        if code != 0:
            code2, _o2, err2 = _run(["worktree", "add", "--force", str(self.path), self.branch], self.root)
            if code2 != 0:
                return False, f"could not create integration worktree: {err or err2}"
        return True, ""

    def merge(self, branches):
        """按给定顺序合并分支。返回 dict：{merged:[...], conflict: branch|None, message}。"""
        ok, err = self._ensure_integration_worktree()
        if not ok:
            return {"merged": [], "conflict": None, "error": err}
        merged = []
        for branch in branches:
            code, _o, err = _run(["-c", "user.email=pico@local", "-c", "user.name=pico", "merge", "--no-ff", "-m", f"integrate {branch}", branch], self.path)
            if code != 0:
                _run(["merge", "--abort"], self.path)   # 冲突：撤销这次合并
                conflicts = self._conflict_files()
                return {"merged": merged, "conflict": branch, "conflict_files": conflicts,
                        "error": f"merge conflict on {branch}"}
            merged.append(branch)
        return {"merged": merged, "conflict": None, "error": ""}

    def _conflict_files(self):
        code, out, _ = _run(["diff", "--name-only", "--diff-filter=U"], self.path)
        if code == 0 and out:
            return [line.strip() for line in out.splitlines() if line.strip()]
        return []

    def integrated_diff(self):
        """集成分支相对 base_commit 改了哪些文件。"""
        code, out, _ = _run(["diff", "--name-only", self.base_commit], self.path)
        if code == 0 and out:
            return [line.strip() for line in out.splitlines() if line.strip()]
        return []

    def apply_to_main(self):
        """把集成分支 fast-forward 合并回主工作区当前分支。返回 (ok, message)。"""
        code, out, _ = _run(["-c", "user.email=pico@local", "-c", "user.name=pico", "merge", "--no-ff", "-m", f"apply {self.branch}", self.branch], self.root)
        if code != 0:
            _run(["merge", "--abort"], self.root)
            return False, f"could not apply integration to main: {out}"
        return True, "applied"

    def cleanup(self):
        _run(["worktree", "remove", "--force", str(self.path)], self.root)
        if self.path.exists():
            import shutil
            shutil.rmtree(self.path, ignore_errors=True)
