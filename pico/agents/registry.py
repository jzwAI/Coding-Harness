"""角色注册表：从 Markdown 文件加载 agent 角色定义。

一个"角色"描述一类子 agent 能干什么：能用哪些工具、最多走几步、
能不能写文件、用多少预算、用哪个模型，以及一段写进 prompt 的工作守则。

角色文件是带 frontmatter 的 Markdown：

    ---
    name: explorer
    description: 只读调查代码
    tools: [list_files, read_file, search]
    max_steps: 8
    write_scope: none        # none | declared | worktree
    budget_ratio: 0.25
    model: inherit           # inherit | provider:model
    ---
    你是 explorer。只做调查，不修改任何文件……

内置角色在 builtin/ 目录；用户可在 .pico/agents/ 下新增或覆盖同名角色。
frontmatter 自己解析，只支持 `key: value` 和 `[a, b]` 两种写法，保持零依赖。
"""

from dataclasses import dataclass, field
from pathlib import Path

LEGAL_TOOLS = {"list_files", "read_file", "search", "run_shell", "write_file", "patch_file"}
WRITE_SCOPE_MODES = {"none", "declared", "worktree"}


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    tools: tuple
    max_steps: int
    write_scope_mode: str          # none | declared | worktree
    budget_ratio: float
    model: str                     # inherit | provider:model
    instructions: str

    @property
    def read_only(self):
        return self.write_scope_mode == "none"

    def to_dict(self):
        return {
            "name": self.name,
            "description": self.description,
            "tools": list(self.tools),
            "max_steps": self.max_steps,
            "write_scope_mode": self.write_scope_mode,
            "budget_ratio": self.budget_ratio,
            "model": self.model,
        }


def _strip_inline_comment(value):
    # 去掉 ` # ...` 行内注释，但不碰引号里的 #。只支持无引号的简单标量，够用。
    out = []
    for i, ch in enumerate(value):
        if ch == "#" and (i == 0 or value[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).strip()


def _parse_scalar(value):
    value = _strip_inline_comment(value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_value(value):
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(item) for item in inner.split(",") if _parse_scalar(item)]
    return _parse_scalar(value)


def parse_frontmatter(text):
    """解析带 frontmatter 的 Markdown，返回 (meta_dict, body_str)。

    frontmatter 是开头两行 `---` 之间的内容；没有 frontmatter 时 meta 为空、
    body 为全文。
    """
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    meta = {}
    body_start = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            body_start = index + 1
            break
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"invalid frontmatter line (no colon): {line!r}")
        key, _, value = line.partition(":")
        meta[key.strip()] = _parse_value(value)
    if body_start is None:
        raise ValueError("frontmatter opened with '---' but never closed")
    body = "\n".join(lines[body_start:]).strip()
    return meta, body


def role_from_text(text, *, source=""):
    meta, body = parse_frontmatter(text)
    name = str(meta.get("name", "")).strip()
    if not name:
        raise ValueError(f"role missing 'name' ({source})")

    tools_raw = meta.get("tools", [])
    if isinstance(tools_raw, str):
        tools_raw = [tools_raw] if tools_raw.strip() else []
    tools = tuple(str(t).strip() for t in tools_raw if str(t).strip())
    unknown = [t for t in tools if t not in LEGAL_TOOLS]
    if unknown:
        raise ValueError(f"role {name!r} lists unknown tools: {', '.join(unknown)} ({source})")

    write_scope_mode = str(meta.get("write_scope", "none")).strip() or "none"
    if write_scope_mode not in WRITE_SCOPE_MODES:
        raise ValueError(f"role {name!r} has invalid write_scope {write_scope_mode!r} ({source})")

    # 只读角色（write_scope: none）不该出现写类工具，避免定义自相矛盾。
    if write_scope_mode == "none":
        write_tools = [t for t in tools if t in {"write_file", "patch_file"}]
        if write_tools:
            raise ValueError(
                f"role {name!r} is read-only (write_scope: none) but lists write tools: {', '.join(write_tools)} ({source})"
            )

    try:
        max_steps = int(meta.get("max_steps", 6))
    except (TypeError, ValueError):
        raise ValueError(f"role {name!r} has non-integer max_steps ({source})")
    if max_steps < 1:
        raise ValueError(f"role {name!r} max_steps must be >= 1 ({source})")

    try:
        budget_ratio = float(meta.get("budget_ratio", 1.0))
    except (TypeError, ValueError):
        raise ValueError(f"role {name!r} has non-numeric budget_ratio ({source})")
    if not (0.0 < budget_ratio <= 1.0):
        raise ValueError(f"role {name!r} budget_ratio must be in (0, 1] ({source})")

    model = str(meta.get("model", "inherit")).strip() or "inherit"

    return Role(
        name=name,
        description=str(meta.get("description", "")).strip(),
        tools=tools,
        max_steps=max_steps,
        write_scope_mode=write_scope_mode,
        budget_ratio=budget_ratio,
        model=model,
        instructions=body,
    )


class RoleRegistry:
    """加载并缓存角色定义。

    加载顺序：先 builtin/ 目录，再用户目录（.pico/agents/）覆盖同名角色。
    """

    def __init__(self, roles):
        self._roles = dict(roles)

    def __contains__(self, name):
        return name in self._roles

    def __getitem__(self, name):
        return self._roles[name]

    def get(self, name, default=None):
        return self._roles.get(name, default)

    def names(self):
        return sorted(self._roles)

    @classmethod
    def _load_dir(cls, directory):
        roles = {}
        directory = Path(directory)
        if not directory.is_dir():
            return roles
        for path in sorted(directory.glob("*.md")):
            role = role_from_text(path.read_text(encoding="utf-8"), source=str(path))
            roles[role.name] = role
        return roles

    @classmethod
    def load(cls, user_dir=None, builtin_dir=None):
        builtin_dir = Path(builtin_dir) if builtin_dir else Path(__file__).parent / "builtin"
        roles = cls._load_dir(builtin_dir)
        if user_dir is not None:
            roles.update(cls._load_dir(user_dir))   # 用户目录覆盖同名内置角色
        return cls(roles)
