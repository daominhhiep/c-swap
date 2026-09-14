"""OpenAI Codex CLI (ChatGPT login) account support.

Everything Codex-specific lives in this package so the Claude path
(``claude_swap.claude.switcher`` and friends) stays untouched. The CLI routes
``--provider codex`` / ``ccswap codex <verb>`` to
:class:`claude_swap.codex.switcher.CodexAccountSwitcher`, which stores its
slots under ``<backup root>/codex/`` and shares the generic helpers
(``UsageStore``, ``FileLock``, ``printer``, ``json_output``) with the Claude
switcher.
"""
