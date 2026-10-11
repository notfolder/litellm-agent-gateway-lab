"""User-Agent からクライアントの種類を決める（審査のガードレールと監査ログで共通に使う）。"""

from __future__ import annotations

import re


def is_claude_code(ua: str) -> bool:
    try:
        from litellm.llms.anthropic.common_utils import is_claude_code_user_agent
        return bool(ua) and is_claude_code_user_agent(ua)
    except ImportError:  # 古い版への備え
        return (ua or "").startswith(("claude-cli/", "claude-code/"))


def client_kind(ua: str) -> str:
    """codex / claude-code / other / unknown（User-Agent が空）。"""
    if is_claude_code(ua):
        return "claude-code"
    try:
        from litellm.litellm_core_utils.core_helpers import is_codex_user_agent
        if ua and is_codex_user_agent(ua):
            return "codex"
    except ImportError:
        if re.match(r"^codex[-_ /]", ua or "", re.I):
            return "codex"
    return "other" if ua else "unknown"
