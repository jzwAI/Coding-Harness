"""验收命令执行：跨平台、不经过 shell。

与 evaluation.evaluator._run_verifier 同源：verifier 命令按 POSIX 规则拆成 argv，
把 python/python3 换成当前解释器，不经 shell 直接执行；只有用到管道/重定向这类
shell 语法时才退回 shell=True。这样 Windows 的 cmd 引号问题不会影响验收。
"""

from __future__ import annotations

import shlex
import subprocess
import sys

_SHELL_OPERATORS = {"&&", "||", "|", ";", ">", ">>", "<"}


def run_verify(command, cwd):
    """执行一条验收命令，返回 (exit_code, output)。"""
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        argv = []
    try:
        if argv and not any(token in _SHELL_OPERATORS for token in argv):
            if argv[0] in {"python", "python3"}:
                argv[0] = sys.executable
            proc = subprocess.run(
                argv, cwd=str(cwd), capture_output=True, text=True,
                encoding="utf-8", errors="replace", check=False, timeout=120,
            )
        else:
            proc = subprocess.run(
                command.replace("python3", sys.executable), cwd=str(cwd), shell=True,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                check=False, timeout=120,
            )
    except subprocess.TimeoutExpired:
        return 124, "verify timed out after 120s"
    except Exception as exc:  # noqa: BLE001
        return 1, f"verify could not run: {exc}"
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, output[:2000]


def run_all(commands, cwd):
    """依次执行所有验收命令，全部退出码为 0 才算通过。返回 (passed, records)。"""
    records = []
    passed = True
    for command in commands:
        code, output = run_verify(command, cwd)
        records.append({"cmd": command, "exit_code": code, "output": output})
        if code != 0:
            passed = False
    return passed, records
