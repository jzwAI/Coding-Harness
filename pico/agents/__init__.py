"""Agent 角色定义与注册表。"""

from .registry import Role, RoleRegistry, parse_frontmatter, role_from_text

__all__ = ["Role", "RoleRegistry", "parse_frontmatter", "role_from_text"]
