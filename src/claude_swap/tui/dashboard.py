"""Dashboard: static account overview on top, a nested action menu below.

The accounts panel is the monitor (active account full-size, others as
one-line minis); the arrow keys drive the *menu*, not the accounts. Anything
account-targeted opens a context of its own:

- ``s`` / menu "Switch account" → :class:`SwitchScreen` — every account
  full-size, Enter switches, pops back.
- ``w`` / menu "Watch accounts" / ``ccswap watch`` → :class:`WatchScreen` —
  the same full cards but read-only: a live monitor. ``s`` arms selection
  (cursor appears on the active account), Enter switches and *stays
  watching*, Esc disarms.
- "Remove account" nests into a submenu listing the accounts.

No global command palette: actions live where their context is.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, ListView, Static

from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.tui.widgets import AccountItem, AccountsPanel, MenuItem, SectionItem

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

FLASH_S = 1.5  # how long a just-refreshed row stays highlighted

MenuEntries = list[tuple[str, str]]  # (label, action_id)

_BACK = ("← back", "back")


class DashboardScreen(Screen):
    BINDINGS = [
        Binding("s", "open_switch", "Switch accounts"),
        Binding("w", "app.open_watch", "Watch"),
        Binding("escape,left", "menu_back", "Back", show=False),
        Binding("q", "app.quit", "Quit"),
        # Power shortcuts; the menu is the discoverable path.
        Binding("g", "app.open_auto", "Auto view", show=False),
        Binding("f", "app.refresh_full", "Refresh usage", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        # Stack of (title, entries); depth 1 = root menu.
        self._menu_stack: list[tuple[str, MenuEntries]] = []

    def compose(self) -> ComposeResult:
        yield AccountsPanel(id="accounts-panel")
        yield Static("", id="menu-title")
        yield ListView(id="menu")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#menu", ListView).focus()
        await self._push_menu("menu", self._root_entries())

    # -- menu plumbing --------------------------------------------------------

    def _root_entries(self) -> MenuEntries:
        # No "Refresh" entry: every view auto-refreshes, so a menu item would
        # wrongly imply the user has to. `f` stays as a hidden escape hatch.
        return [
            ("Switch account…", "switch"),
            ("Watch accounts", "watch"),
            ("Auto-switch view", "auto"),
            ("Add account…", "add-menu"),
            ("Disable / enable account…", "disable-menu"),
            ("Remove account…", "remove-menu"),
            ("Theme…", "theme-menu"),
            ("Quit", "quit"),
        ]

    def _add_entries(self) -> MenuEntries:
        entries: MenuEntries = [
            ("From current Claude Code login", "add-login"),
            ("From a setup-token / API key…", "add-token"),
        ]
        if self.app.codex_switcher is not None:
            entries.append(("From current Codex CLI login", "add-codex"))
        entries.append(_BACK)
        return entries

    def _remove_entries(self) -> MenuEntries:
        entries: MenuEntries = []
        for acc in self.app.all_accounts():
            # Claude rows keep the bare `remove:N` id; Codex rows carry their
            # provider since slot numbers repeat across the two groups.
            codex = acc.provider == "codex"
            entries.append(
                (
                    f"{'Codex ' if codex else ''}{acc.number}  "
                    f"{f'{acc.alias} ({acc.email})' if acc.alias else acc.email}"
                    f"  [{acc.display_tag}]",
                    f"remove:codex:{acc.number}" if codex else f"remove:{acc.number}",
                )
            )
        entries.append(_BACK)
        return entries

    def _disable_entries(self) -> MenuEntries:
        """One row per account, labelled with its current state and the action
        selecting it will take (enable a disabled one, disable an active one)."""
        snap = self.app.snapshot
        entries: MenuEntries = []
        for acc in (snap.accounts if snap else ()):
            name = f"{acc.alias} ({acc.email})" if acc.alias else acc.email
            action = "→ enable" if acc.disabled else "→ disable"
            state = "  (disabled)" if acc.disabled else ""
            entries.append(
                (f"{acc.number}  {name}{state}   {action}", f"disable:{acc.number}")
            )
        entries.append(_BACK)
        return entries

    def _theme_entries(self) -> MenuEntries:
        """dark / light / auto, with the active setting marked."""
        current = self.app._theme_name
        entries: MenuEntries = [
            (f"{'●' if name == current else ' '} {name}", f"theme:{name}")
            for name in ("dark", "light", "auto")
        ]
        entries.append(_BACK)
        return entries

    async def _push_menu(self, title: str, entries: MenuEntries) -> None:
        self._menu_stack.append((title, entries))
        await self._render_menu()

    async def _pop_menu(self) -> None:
        if len(self._menu_stack) > 1:
            self._menu_stack.pop()
            await self._render_menu()

    async def _render_menu(self) -> None:
        title, entries = self._menu_stack[-1]
        crumb = " › ".join(t for t, _ in self._menu_stack)
        self.query_one("#menu-title", Static).update(crumb)
        menu = self.query_one("#menu", ListView)
        await menu.clear()
        await menu.extend(
            MenuItem(label, action_id, muted=(action_id == "back"))
            for label, action_id in entries
        )
        menu.index = 0

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, MenuItem):
            await self._dispatch(item.action_id)

    async def _dispatch(self, action_id: str) -> None:
        app = self.app
        actions: dict[str, Callable[[], None]] = {
            "switch": self.action_open_switch,
            "watch": app.action_open_watch,
            "auto": app.action_open_auto,
            "add-login": app.action_add_current,
            "add-token": app.action_add_token,
            "add-codex": app.action_add_codex_current,
            "quit": app.exit,
        }
        if action_id == "back":
            await self._pop_menu()
        elif action_id == "add-menu":
            await self._push_menu("add account", self._add_entries())
        elif action_id == "remove-menu":
            await self._push_menu("remove account", self._remove_entries())
        elif action_id.startswith("remove:"):
            parts = action_id.split(":")
            provider, number = (parts[1], parts[2]) if len(parts) == 3 else ("claude", parts[1])
            email = next(
                (
                    a.email
                    for a in app.all_accounts()
                    if a.number == number and a.provider == provider
                ),
                "?",
            )
            app.confirm_remove(number, email, provider)
        elif action_id == "theme-menu":
            await self._push_menu("theme", self._theme_entries())
        elif action_id.startswith("theme:"):
            name = action_id.split(":", 1)[1]
            app.apply_theme(name)
            app.notify(f"Theme: {name}")
            await self._pop_menu()
        elif action_id == "disable-menu":
            await self._push_menu("disable / enable", self._disable_entries())
        elif action_id.startswith("disable:"):
            number = action_id.split(":", 1)[1]
            app.do_toggle_disabled(number)
            await self._pop_menu()
        else:
            actions[action_id]()

    # -- actions ----------------------------------------------------------------

    def action_open_switch(self) -> None:
        if not isinstance(self.app.screen, SwitchScreen):
            self.app.push_screen(SwitchScreen())

    async def action_menu_back(self) -> None:
        await self._pop_menu()

    def action_cursor_down(self) -> None:
        self.query_one("#menu", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#menu", ListView).action_cursor_up()


class AccountListScreen(Screen):
    """Shared machinery: a live ListView of full account cards.

    Subclasses decide what the cursor does — :class:`SwitchScreen` is
    selection-first, :class:`WatchScreen` is a monitor that can arm
    selection on demand.
    """

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._keys: list[str] = []
        self._stamps: dict[str, float | None] = {}

    def compose(self) -> ComposeResult:
        yield Static("", id="list-title")
        yield ListView(id="accounts")
        yield Footer()

    def on_mount(self) -> None:
        self.watch(self.app, "snapshot", self._on_snapshot)
        self.watch(self.app, "codex_snapshot", self._on_codex_snapshot)

    async def _on_snapshot(self, snap: AccountsSnapshot | None) -> None:
        if snap is None:
            return
        await self._rebuild()

    async def _on_codex_snapshot(self, snap: AccountsSnapshot | None) -> None:
        if self.app.snapshot is None:
            return  # the Claude snapshot's arrival builds the list
        await self._rebuild()

    def _rows(self) -> list[AccountSnapshot | str]:
        """Claude accounts, then — when there are any — a "Codex" section
        header and the Codex accounts. The header is a plain string; the
        Claude group needs none since it is where the list always starts."""
        app = self.app
        rows: list[AccountSnapshot | str] = list(app.snapshot.accounts) if app.snapshot else []
        codex = app.codex_snapshot
        if codex is not None and codex.accounts:
            rows.append("Codex")
            rows.extend(codex.accounts)
        return rows

    async def _rebuild(self) -> None:
        listview = self.query_one("#accounts", ListView)
        rows = self._rows()
        keys = [row.key if isinstance(row, AccountSnapshot) else f"#{row}" for row in rows]
        accounts = [row for row in rows if isinstance(row, AccountSnapshot)]
        if keys != self._keys:
            first_build = not self._keys
            previous = listview.index
            await listview.clear()
            await listview.extend(
                AccountItem(row) if isinstance(row, AccountSnapshot) else SectionItem(row)
                for row in rows
            )
            self._keys = keys
            listview.index = (
                self._index_after_build(rows, first_build, previous)
                if accounts
                else None
            )
        else:
            for item, acc in zip(listview.query(AccountItem), accounts):
                item.set_account(acc)
        self._flash_updated(accounts, listview)

    def _index_after_build(
        self, rows: list, first_build: bool, previous: int | None
    ) -> int | None:
        """Where the cursor lands after the list is (re)built."""
        if first_build:
            return self._active_index(rows)
        index = min(previous or 0, len(rows) - 1)
        if not isinstance(rows[index], AccountSnapshot):
            index = self._active_index(rows)
        return index

    def _active_index(self, rows: list | None = None) -> int:
        """List index of the active Claude account (else the first account)."""
        if rows is None:
            rows = self._rows()
        snap = self.app.snapshot
        active = snap.active_number if snap else None
        first_account = next(
            (i for i, row in enumerate(rows) if isinstance(row, AccountSnapshot)), 0
        )
        return next(
            (
                i
                for i, row in enumerate(rows)
                if isinstance(row, AccountSnapshot)
                and row.provider == "claude"
                and row.number == active
            ),
            first_account,
        )

    def _flash_updated(self, accounts: list[AccountSnapshot], listview: ListView) -> None:
        """Briefly highlight rows whose stored measurement just advanced."""
        new_stamps = {acc.key: acc.usage.fetched_at for acc in accounts}
        if self._stamps:
            changed = {
                key
                for key, ts in new_stamps.items()
                if ts is not None and ts != self._stamps.get(key)
            }
            for item in listview.query(AccountItem):
                if item.key in changed and not item.has_class("flash"):
                    item.add_class("flash")
                    self.set_timer(FLASH_S, partial(item.remove_class, "flash"))
        self._stamps = new_stamps

    def action_cursor_down(self) -> None:
        self.query_one("#accounts", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#accounts", ListView).action_cursor_up()


class SwitchScreen(AccountListScreen):
    """All accounts, full-size and alive: arrows pick, Enter switches."""

    BINDINGS = [
        # priority: outranks the focused ListView's own (hidden) enter binding
        # so "Switch" is visible in the footer; the action delegates right back
        # to the list cursor, so behavior is identical.
        Binding("enter", "select_highlighted", "Switch", priority=True),
        Binding("b", "app.switch_best", "Best pick"),
        Binding("escape,q,s", "back", "Back"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def on_mount(self) -> None:
        self.query_one("#list-title", Static).update("switch to which account?")
        self.query_one("#accounts", ListView).focus()
        super().on_mount()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, AccountItem):
            self.app.do_switch(item.number, item.provider)
            self.app.pop_screen()

    def action_select_highlighted(self) -> None:
        listview = self.query_one("#accounts", ListView)
        if listview.display:
            listview.action_select_cursor()

    def action_back(self) -> None:
        self.app.pop_screen()


class WatchScreen(AccountListScreen):
    """Live monitor of every account, full detail, hands-off by default.

    ``s`` arms selection (cursor appears on the active account); Enter then
    switches and stays here — you keep watching on the new account. Esc
    disarms selection first, then leaves the screen.
    """

    _WATCH_TITLE = "watching all accounts"
    _SELECT_TITLE = "switch to which account? · enter confirm · esc cancel"

    BINDINGS = [
        Binding("s", "toggle_select", "Switch"),
        Binding("enter", "select_highlighted", "Confirm", priority=True),
        Binding("f", "app.refresh_full", "Refresh", show=False),
        Binding("escape,q", "back", "Back"),
        Binding("down,j", "nav_down", show=False),
        Binding("up,k", "nav_up", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._selecting = False

    def on_mount(self) -> None:
        self.watch(self.app, "refresh_status", self._on_refresh_status)
        self.query_one("#list-title", Static).update(self._title_text())
        super().on_mount()

    def _title_text(self) -> str:
        if self._selecting:
            return self._SELECT_TITLE
        status = self.app.refresh_status
        return f"{self._WATCH_TITLE} · {status}" if status else self._WATCH_TITLE

    def _on_refresh_status(self, status: str) -> None:
        if not self._selecting:
            self.query_one("#list-title", Static).update(self._title_text())

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "select_highlighted" and not self._selecting:
            return False  # hidden and inert until selection is armed
        return True

    def _index_after_build(
        self, rows: list, first_build: bool, previous: int | None
    ) -> int | None:
        if not self._selecting:
            return None  # monitor mode: no cursor at all
        return super()._index_after_build(rows, first_build, previous)

    def _set_selecting(self, on: bool) -> None:
        self._selecting = on
        listview = self.query_one("#accounts", ListView)
        title = self.query_one("#list-title", Static)
        if on:
            if any(isinstance(row, AccountSnapshot) for row in self._rows()):
                listview.index = self._active_index()
            listview.focus()
            title.update(self._SELECT_TITLE)
        else:
            listview.index = None
            self.set_focus(None)
            title.update(self._title_text())
        self.refresh_bindings()

    def action_toggle_select(self) -> None:
        self._set_selecting(not self._selecting)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if not self._selecting:
            return  # e.g. a stray click while just watching
        item = event.item
        if isinstance(item, AccountItem):
            self.app.do_switch(item.number, item.provider)
            self._set_selecting(False)  # stay here, keep watching

    def action_select_highlighted(self) -> None:
        if self._selecting:
            self.query_one("#accounts", ListView).action_select_cursor()

    def action_back(self) -> None:
        if self._selecting:
            self._set_selecting(False)
        else:
            self.app.pop_screen()

    def action_nav_down(self) -> None:
        listview = self.query_one("#accounts", ListView)
        if self._selecting:
            listview.action_cursor_down()
        else:
            listview.scroll_down(animate=False)

    def action_nav_up(self) -> None:
        listview = self.query_one("#accounts", ListView)
        if self._selecting:
            listview.action_cursor_up()
        else:
            listview.scroll_up(animate=False)
