"""Boot sequence — the same beat as tuigreet -> session-chooser, in-app.

Runs the first real Tailscale poll while the log scrolls, so the main screen
comes up already populated. Any key skips to the main screen.
"""
from __future__ import annotations

import asyncio
import shutil

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import RichLog, Static

from . import __version__
from .inventory import Inventory, pretty_path
from .probes import TSStatus, tailscale_status
from .theme import DIM, ORANGE, PEACH, RED
from .wordart import WORDMARK, submark, wordmark


class BootScreen(Screen[TSStatus | None]):
    """Returns the initial TSStatus (or None if it never ran) on dismiss."""

    BINDINGS = [("escape,enter,space", "skip", "skip")]

    def __init__(self, inv: Inventory) -> None:
        super().__init__()
        self.inv = inv
        self._ts: TSStatus | None = None
        self._done = False
        self._dismissed = False

    def compose(self) -> ComposeResult:
        with Vertical(id="boot"):
            with Center():
                yield Static(wordmark(0), id="boot-mark")
            with Center():
                yield Static(submark(), id="boot-submark")
            with Center():
                yield Static(
                    Text(f"TERMINAL  v{__version__}   //   {self.inv.squadron}", style=DIM),
                    id="boot-sub",
                )
            yield RichLog(id="boot-log", markup=True, highlight=False, wrap=True)
            with Center():
                yield Static("", id="boot-hint")

    def on_mount(self) -> None:
        self.run_boot()

    async def power_on(self) -> None:
        """Light the wordmark top to bottom, then a quick flicker."""
        mark = self.query_one("#boot-mark", Static)
        for rows in range(1, len(WORDMARK) + 1):
            mark.update(wordmark(rows))
            await asyncio.sleep(0.06)
        for rows in (3, len(WORDMARK), 7, len(WORDMARK)):
            mark.update(wordmark(rows))
            await asyncio.sleep(0.045)

    def _line(self, state: str, label: str, detail: str = "") -> None:
        log = self.query_one("#boot-log", RichLog)
        colour = {"OK": ORANGE, "..": DIM, "!!": RED}.get(state, PEACH)
        dots = "." * max(2, 16 - len(label))
        log.write(f"[{colour}]\\[ {state} ][/] [{PEACH}]{label}[/] [{DIM}]{dots}[/] {detail}")

    @work(exclusive=True)
    async def run_boot(self) -> None:
        inv = self.inv
        await self.power_on()
        await asyncio.sleep(0.2)
        self._line("OK", "inventory", f"{len(inv.units)} units  [{DIM}]{pretty_path(inv.source)}[/]")
        await asyncio.sleep(0.18)

        self._line("..", "tailscale", "handshake")
        self._ts = await tailscale_status()
        if self._ts.ok:
            online = sum(1 for u in inv.units if (p := self._ts.peers.get(u.tailscale)) and p.online)
            self._line("OK", "tailscale", f"link up · [{ORANGE}]{online}[/]/{len(inv.units)} units online")
        else:
            self._line("!!", "tailscale", f"[{RED}]{self._ts.error}[/]")
        await asyncio.sleep(0.18)

        self._line("OK" if shutil.which("ssh") else "!!", "openssh",
                   "client ready" if shutil.which("ssh") else f"[{RED}]ssh not on PATH[/]")
        await asyncio.sleep(0.15)

        if inv.proxmox:
            self._line("OK", "proxmox", f"api {inv.proxmox.host}")
        else:
            self._line("..", "proxmox", "not configured")
        await asyncio.sleep(0.15)

        self._line("OK", "telemetry", f"probing every {inv.probe_interval}s")
        await asyncio.sleep(0.35)

        log = self.query_one("#boot-log", RichLog)
        log.write("")
        log.write(f"[bold {ORANGE}]Welcome back, {inv.callsign}.[/]")
        self._done = True
        self.query_one("#boot-hint", Static).update(Text("▸ press any key", style=DIM))
        await asyncio.sleep(1.4)
        self._finish()

    def _finish(self) -> None:
        if self._dismissed:
            return
        self._dismissed = True
        self.dismiss(self._ts)

    def action_skip(self) -> None:
        self._finish()

    def on_key(self) -> None:
        if self._done:
            self._finish()
