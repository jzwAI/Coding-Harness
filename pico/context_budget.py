"""上下文预算配置：把"模型上下文窗口"翻译成 ContextManager 能直接用的预算。

为什么存在：
旧实现把预算写死成 12400 字符（约 3~4k token），这个尺寸适合本地小模型，
但对 128k+ 窗口的远程模型来说太保守：大文件、跨文件任务的信息会被提前裁掉。
这个模块负责三件事：

1. `estimate_tokens()`：零依赖的 token 估算（中日韩字符按 1 token/字，
   其他字符按 3 字符/token，故意偏保守，宁可多算不要少算）。
2. `COMPACT_PROFILE`：与旧版完全一致的预算，作为默认值，保证老测试/老评测不变。
3. `build_budget_profile()`：根据上下文窗口和输出 token 数，按比例放大各段预算，
   同时放大历史窗口、单条历史上限和工具输出上限，否则"预算变大但内容进不来"。

预算内部仍以"字符"为渲染单位（裁剪精确、可复现），
token 预算作为第二道上限，在 ContextManager 里做最终校验。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace

CHARS_PER_TOKEN = 3.0
SAFETY_MARGIN_TOKENS = 1024
DEFAULT_FILL_RATIO = 0.6
DEFAULT_MAX_PROMPT_TOKENS = 32000
MIN_PROMPT_TOKENS = 1000

# 旧版（compact）预算，数值与历史实现保持一致。
DEFAULT_TOTAL_BUDGET = 12400
DEFAULT_SECTION_BUDGETS = {
    "prefix": 4000,
    "memory": 1600,
    "relevant_memory": 1200,
    "history": 5200,
}
DEFAULT_RECENT_WINDOW = 6
DEFAULT_RECENT_LINE_LIMIT = 900
DEFAULT_OLDER_LINE_LIMIT = 60
DEFAULT_TOOL_OUTPUT_CHARS = 4000

# 放大时各段占总预算的比例：历史是多轮任务里增长最快、也最有价值的部分。
SECTION_RATIOS = {
    "prefix": 0.25,
    "memory": 0.10,
    "relevant_memory": 0.07,
    "history": 0.58,
}

_CJK_PATTERN = re.compile(
    r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]"
)


def estimate_tokens(text):
    """粗估 token 数。只用于预算判断，不追求和具体 tokenizer 完全一致。"""
    text = str(text or "")
    if not text:
        return 0
    cjk = len(_CJK_PATTERN.findall(text))
    other = len(text) - cjk
    return cjk + math.ceil(other / CHARS_PER_TOKEN)


def _clamp(value, low, high):
    return max(low, min(high, int(value)))


@dataclass
class BudgetProfile:
    name: str
    total_chars: int
    section_budgets: dict = field(default_factory=lambda: dict(DEFAULT_SECTION_BUDGETS))
    recent_window: int = DEFAULT_RECENT_WINDOW
    recent_line_limit: int = DEFAULT_RECENT_LINE_LIMIT
    older_line_limit: int = DEFAULT_OLDER_LINE_LIMIT
    tool_output_chars: int = DEFAULT_TOOL_OUTPUT_CHARS
    token_budget: int | None = None
    context_window_tokens: int | None = None
    max_new_tokens: int | None = None

    def to_dict(self):
        return {
            "name": self.name,
            "total_chars": self.total_chars,
            "section_budgets": dict(self.section_budgets),
            "recent_window": self.recent_window,
            "recent_line_limit": self.recent_line_limit,
            "older_line_limit": self.older_line_limit,
            "tool_output_chars": self.tool_output_chars,
            "token_budget": self.token_budget,
            "context_window_tokens": self.context_window_tokens,
            "max_new_tokens": self.max_new_tokens,
        }


COMPACT_PROFILE = BudgetProfile(name="compact", total_chars=DEFAULT_TOTAL_BUDGET)


def compact_profile():
    """返回一份旧版预算的独立副本，避免调用方误改全局默认值。"""
    return replace(COMPACT_PROFILE, section_budgets=dict(DEFAULT_SECTION_BUDGETS))


def build_budget_profile(
    context_window_tokens,
    max_new_tokens,
    max_prompt_tokens=DEFAULT_MAX_PROMPT_TOKENS,
    fill_ratio=DEFAULT_FILL_RATIO,
):
    """根据模型窗口生成预算。

    计算方式：
        可用 = 窗口 - 输出 token - 安全余量
        prompt token 预算 = min(可用 × fill_ratio, max_prompt_tokens)

    - fill_ratio < 1：token 估算有误差，且长 prompt 会拉高延迟和费用，所以不把窗口塞满。
    - max_prompt_tokens：成本上限。每一步都会重发整段 prompt，
      步数 × prompt 长度 才是一次任务真正的输入 token 消耗。
    - 如果算出来的预算还不如旧版大（本地小窗口模型），就沿用 compact 布局，
      只额外加上 token 上限，防止中文内容把小窗口撑爆。
    """
    window = int(context_window_tokens)
    output_tokens = max(0, int(max_new_tokens))
    if window <= 0:
        raise ValueError("context_window_tokens must be positive")
    available = window - output_tokens - SAFETY_MARGIN_TOKENS
    prompt_tokens = int(available * float(fill_ratio))
    if max_prompt_tokens:
        prompt_tokens = min(prompt_tokens, int(max_prompt_tokens))
    if prompt_tokens < MIN_PROMPT_TOKENS:
        raise ValueError(
            f"context window {window} is too small for max_new_tokens={output_tokens}; "
            f"prompt budget would be {prompt_tokens} tokens (< {MIN_PROMPT_TOKENS})"
        )

    legacy_tokens = int(DEFAULT_TOTAL_BUDGET / CHARS_PER_TOKEN)
    if prompt_tokens <= legacy_tokens:
        return replace(
            compact_profile(),
            token_budget=prompt_tokens,
            context_window_tokens=window,
            max_new_tokens=output_tokens,
        )

    total_chars = int(prompt_tokens * CHARS_PER_TOKEN)
    section_budgets = {section: int(total_chars * ratio) for section, ratio in SECTION_RATIOS.items()}
    history_chars = section_budgets["history"]
    recent_window = _clamp(history_chars // 4000, DEFAULT_RECENT_WINDOW, 24)
    tool_output_chars = _clamp(total_chars // 6, DEFAULT_TOOL_OUTPUT_CHARS, 32000)
    # 单条最近历史的上限要和工具输出上限对齐：否则工具层放进来 16k 的文件内容，
    # 渲染历史时又被截回 2k，工具输出放大就白做了。
    # 上限再压到 history 的 1/3，保证一条大输出不会把最近窗口里其他条目全部挤掉。
    recent_line_limit = _clamp(min(tool_output_chars, history_chars // 3), DEFAULT_RECENT_LINE_LIMIT, 32000)
    return BudgetProfile(
        name="scaled",
        total_chars=total_chars,
        section_budgets=section_budgets,
        recent_window=recent_window,
        recent_line_limit=recent_line_limit,
        older_line_limit=_clamp(history_chars // 400, DEFAULT_OLDER_LINE_LIMIT, 240),
        tool_output_chars=tool_output_chars,
        token_budget=prompt_tokens,
        context_window_tokens=window,
        max_new_tokens=output_tokens,
    )
