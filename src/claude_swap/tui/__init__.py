"""Textual-based interactive TUI for claude-swap.

Entry point for ``ccswap tui`` (and bare ``ccswap`` in an interactive
terminal). Heavy imports (textual, rich) stay inside :func:`run` so the
plain CLI paths — ``ccswap list``, cron's ``ccswap auto --once`` — never pay
for them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_swap.codex.switcher import CodexAccountSwitcher
    from claude_swap.switcher import ClaudeAccountSwitcher


def run(
    switcher: "ClaudeAccountSwitcher",
    start: str = "dashboard",
    codex_switcher: "CodexAccountSwitcher | None" = None,
) -> int:
    """Run the TUI over an existing switcher. Returns the process exit code.

    ``start="watch"`` (the ``ccswap watch`` command) opens directly on the
    live watch page, stacked over the dashboard. ``codex_switcher`` adds the
    managed Codex CLI accounts as their own group in every account view.
    """
    from claude_swap.appearance import detect_terminal_background, drain_stdin
    from claude_swap.tui.app import CswapApp

    # Sense the terminal background while we still own stdin in cooked mode
    # (Textual's driver starts inside app.run()). Always detect so cycling to
    # 'auto' works even when the initial theme is explicit. Both calls are
    # meant to fail safe on their own, but they're wrapped here too: a
    # detection bug must never crash the TUI launch.
    try:
        detected = detect_terminal_background()
    except Exception:
        detected = None
    app = CswapApp(switcher, start=start, detected=detected, codex_switcher=codex_switcher)
    # Drain any late OSC reply immediately before Textual's driver starts,
    # so it isn't reissued as keystrokes once the app takes over the terminal.
    try:
        drain_stdin()
    except Exception:
        pass
    app.run()
    return app.return_code or 0
