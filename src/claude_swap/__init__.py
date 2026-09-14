"""Multi-account switcher for Claude Code and the Codex CLI (ccswap)."""

from importlib.metadata import version

__version__ = version("ccswap")

from claude_swap.claude.switcher import ClaudeAccountSwitcher

__all__ = ["ClaudeAccountSwitcher", "__version__"]
