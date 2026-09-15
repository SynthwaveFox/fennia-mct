"""FENNIA MCT — main screen.

    ╔═ UNITS ═══════════╗╔═ TELEMETRY ══════════════════════════╗
    ║ ● pve01  pve  9ms ║║ pve01 · linux · 100.64.0.1           ║
    ║ ◐ nas    lab  --- ║║ up 41d 3h · load 0.8 0.7 0.6          ║
    ║ ○ ct-101 lab  --- ║║ mem ▰▰▰▰▰▱▱▱ 61%   disk ▰▰▰▱▱▱▱▱ 34%  ║
    ╚═══════════════════╝╚══════════════════════════════════════╝
    ╔═ LOG ═════════════════════════════════════════════════════╗
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import shlex
import subprocess
import sys
import time
from datetime import datetime

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Digits, Footer, Input, RichLog, Static

from . import __version__
from .boot import BootScreen
from .forms import FormResult, UnitForm
from .inventory import Inventory, Site, Unit, load_inventory, pretty_path, save_inventory
from .keys import ssh_identity_args
from .probes import HttpResult, PVEStatus, Probe, TSStatus, http_check, proxmox_status, ssh_probe, tailscale_status
from .theme import AMBER, DIM, FENNIA, GREEN, ORANGE, PEACH, RED, TEXT

GLYPH_UP = "●"
GLYPH_HALF = "◐"
GLYPH_DOWN = "○"
GLYPH_UNKNOWN = "◌"


def gauge(pct: float, width: int = 10) -> Text:
    filled = round(pct / 100 * width)
    colour = ORANGE if pct < 70 else AMBER if pct < 90 else RED
    t = Text()
    t.append("▰" * filled, style=colour)
    t.append("▱" * (width - filled), style=DIM)
    t.append(f" {pct:3.0f}%", style=TEXT)
    return t


class Panel(Static):
    """A bordered block with a title — the gum-style double border lives in CSS."""


class MCT(App[None]):
    TITLE = "FENNIA MCT"
    CSS_PATH = "theme.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("enter", "connect", "ssh"),
        Binding("l", "connect_lan", "ssh via LAN"),
        Binding("x", "connect_via", "exec via host", show=False),
        Binding("r", "refresh", "refresh"),
        Binding("slash", "filter", "filter", key_display="/"),
        Binding("escape", "clear_filter", show=False),
        Binding("t", "tailscale_ping", "ts ping"),
        Binding("a", "add_unit", "add"),
        Binding("e", "edit_unit", "edit"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, inv: Inventory, boot: bool = True) -> None:
        super().__init__()
        self.inv = inv
        self.boot = boot
        self.ts: TSStatus = TSStatus(ok=False, error="not polled yet")
        self.probes: dict[str, Probe] = {}
        self.pve: PVEStatus | None = None
        self.http: dict[str, HttpResult] = {}
        self.filter_text = ""
        self._probing: set[str] = set()
        self.direct_ok: dict[str, bool] = {}   # last probe reached the unit by plain ssh

    # ------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(Text.assemble(("▌", ORANGE), ("FENNIA", f"bold {ORANGE}"), ("▐ ", ORANGE),
                                       ("MASTER CONTROL", f"bold {PEACH}"),
                                       (f"  v{__version__}", DIM)), id="brand")
            yield Static("", id="tsline")
            yield Digits("--:--:--", id="clock")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Input(placeholder="filter units…", id="filter")
                yield DataTable(id="units", cursor_type="row", zebra_stripes=False)
                with Horizontal(id="unit-actions"):
                    yield Button("+ ADD", id="btn-add")
                    yield Button("EDIT", id="btn-edit")
            with Vertical(id="right"):
                yield Panel("", id="detail")
                yield DataTable(id="guests", cursor_type="none", zebra_stripes=False)
        yield RichLog(id="log", markup=True, highlight=False, wrap=False, max_lines=400)
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(FENNIA)
        self.theme = "fennia"
        self.query_one("#units", DataTable).border_title = "UNITS"
        self.query_one("#detail", Panel).border_title = "TELEMETRY"
        self.query_one("#guests", DataTable).border_title = "PROXMOX GUESTS"
        self.query_one("#log", RichLog).border_title = "LOG"
        self.query_one("#filter", Input).display = False

        table = self.query_one("#units", DataTable)
        table.add_column(" ", key="st", width=1)
        table.add_column("UNIT", key="name")
        table.add_column("TAG", key="tag")
        table.add_column("LINK", key="link", width=15)
        table.add_column("RTT", key="rtt", width=7)
        table.add_column("SVC", key="svc", width=5)

        guests = self.query_one("#guests", DataTable)
        guests.add_column(" ", key="st", width=1)
        guests.add_column("ID", key="id", width=4)
        guests.add_column("NAME", key="name")
        guests.add_column("TYPE", key="type", width=4)
        guests.add_column("NODE", key="node")
        guests.add_column("CPU", key="cpu", width=5)
        guests.add_column("MEM", key="mem")
        guests.display = self.inv.proxmox is not None

        self.rebuild_table()
        self.set_interval(1, self.tick_clock)
        self.tick_clock()

        if self.boot:
            self.push_screen(BootScreen(self.inv), callback=self.after_boot)
        else:
            self.after_boot(None)

    def after_boot(self, ts: TSStatus | None) -> None:
        if ts is not None:
            self.apply_tailscale(ts)
        self.log_line("mct", f"online · {len(self.inv.units)} units · inventory {pretty_path(self.inv.source)}")
        self.set_interval(self.inv.tailscale_interval, self.poll_tailscale)
        self.set_interval(self.inv.probe_interval, self.probe_all)
        if self.inv.proxmox:
            self.set_interval(20, self.poll_proxmox)
            self.poll_proxmox()
        if self.inv.all_sites():
            self.set_interval(self.inv.http_interval, self.poll_sites)
            self.poll_sites()
        if ts is None:
            self.poll_tailscale()
        self.probe_all()
        self.query_one("#units", DataTable).focus()

    # ------------------------------------------------------------ helpers

    def log_line(self, src: str, msg: str, level: str = "info") -> None:
        colour = {"info": PEACH, "ok": ORANGE, "warn": AMBER, "err": RED}[level]
        stamp = datetime.now().strftime("%H:%M:%S")
        self.query_one("#log", RichLog).write(f"[{DIM}]{stamp}[/]  [{colour}]{src:<9}[/] {msg}")

    def tick_clock(self) -> None:
        self.query_one("#clock", Digits).update(datetime.now().strftime("%H:%M:%S"))

    @property
    def visible_units(self) -> list[Unit]:
        f = self.filter_text.lower()
        if not f:
            return self.inv.units
        return [u for u in self.inv.units
                if f in u.name.lower() or any(f in t.lower() for t in u.tags) or f in u.kind
                or any(f in x.name.lower() for x in u.sites)]

    @property
    def selected_key(self) -> str | None:
        table = self.query_one("#units", DataTable)
        if table.row_count == 0:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    @property
    def selected(self) -> Unit | None:
        """The highlighted unit — or the parent unit when a site row is highlighted."""
        key = self.selected_key
        if not key:
            return None
        if key.startswith("site:"):
            site = self.inv.site_by_name(key[5:])
            return self.inv.by_name(site.unit) if site and site.unit else None
        return self.inv.by_name(key)

    @property
    def selected_site(self) -> Site | None:
        key = self.selected_key
        return self.inv.site_by_name(key[5:]) if key and key.startswith("site:") else None

    def status_of(self, u: Unit) -> tuple[str, str]:
        """(glyph, colour) for a unit combining tailscale + probe."""
        peer = self.ts.peers.get(u.tailscale) if self.ts.ok else None
        probe = self.probes.get(u.name)
        if probe and probe.ok:
            if probe.services and any(s != "active" for s in probe.services.values()):
                return GLYPH_HALF, AMBER
            if any((r := self.http.get(x.name)) is not None and not r.ok for x in u.sites):
                return GLYPH_HALF, AMBER
            return GLYPH_UP, ORANGE
        if peer is not None:
            if peer.online:
                return (GLYPH_HALF, PEACH) if probe is None else (GLYPH_HALF, AMBER)
            return GLYPH_DOWN, DIM
        if probe is not None and not probe.ok:
            return GLYPH_DOWN, DIM
        return GLYPH_UNKNOWN, DIM

    # ------------------------------------------------------------ table

    def rebuild_table(self) -> None:
        table = self.query_one("#units", DataTable)
        current = None
        if table.row_count:
            current = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        table.clear()
        f = self.filter_text.lower()
        for u in self.visible_units:
            table.add_row(*self.row_cells(u), key=u.name)
            kids = [x for x in u.sites if not f or f in x.name.lower() or f in u.name.lower()
                    or any(f in t.lower() for t in u.tags) or f in u.kind]
            for i, x in enumerate(kids):
                table.add_row(*self.site_cells(x, last=(i == len(kids) - 1)), key=f"site:{x.name}")
        loose = [x for x in self.inv.sites if not f or f in x.name.lower()]
        for i, x in enumerate(loose):
            table.add_row(*self.site_cells(x, last=(i == len(loose) - 1), orphan=True), key=f"site:{x.name}")
        if current:
            try:
                idx = table.get_row_index(current)
                table.move_cursor(row=idx)
            except Exception:  # noqa: BLE001 — row filtered out
                pass
        self.render_detail()

    def row_cells(self, u: Unit) -> list[Text | str]:
        glyph, colour = self.status_of(u)
        peer = self.ts.peers.get(u.tailscale) if self.ts.ok else None
        probe = self.probes.get(u.name)
        name = Text(u.name, style=f"bold {TEXT}" if glyph != GLYPH_DOWN else DIM)
        tag = Text(u.tags[0] if u.tags else u.kind, style=PEACH)
        if peer is None:
            link = Text("—", style=DIM)
        elif not peer.online:
            link = Text("offline", style=DIM)
        elif peer.relay and not peer.ip:
            link = Text(f"relay {peer.relay}", style=AMBER)
        else:
            link = Text(peer.ip or "direct", style=DIM)
        if probe is None:
            rtt = Text("…" if u.name in self._probing else "—", style=DIM)
        elif probe.ok:
            rtt = Text(f"{probe.latency_ms}ms", style=ORANGE if probe.latency_ms < 300 else AMBER)
        else:
            rtt = Text("✕", style=RED)
        if probe and probe.ok and probe.services:
            good = sum(1 for s in probe.services.values() if s == "active")
            total = len(probe.services)
            svc = Text(f"{good}/{total}", style=ORANGE if good == total else RED)
        else:
            svc = Text("—", style=DIM)
        return [Text(glyph, style=colour), name, tag, link, rtt, svc]

    def refresh_row(self, u: Unit) -> None:
        table = self.query_one("#units", DataTable)
        try:
            table.get_row_index(u.name)
        except Exception:  # noqa: BLE001
            return
        for col, cell in zip(("st", "name", "tag", "link", "rtt", "svc"), self.row_cells(u)):
            table.update_cell(u.name, col, cell)

    @on(DataTable.RowHighlighted, "#units")
    def _on_highlight(self) -> None:
        self.render_detail()

    @on(DataTable.RowSelected, "#units")
    def _on_select(self) -> None:
        self.action_connect()

    # ------------------------------------------------------------ detail

    def render_site_detail(self, x: Site) -> None:
        panel = self.query_one("#detail", Panel)
        r = self.http.get(x.name)
        t = Text()
        if r is None:
            t.append(f"{GLYPH_UNKNOWN} ", style=DIM)
        else:
            t.append(f"{GLYPH_UP if r.ok else GLYPH_DOWN} ", style=ORANGE if r.ok else RED)
        t.append(x.name.upper(), style=f"bold {ORANGE}")
        t.append("   site", style=PEACH)
        if x.unit:
            t.append(f"   on {x.unit}", style=DIM)
        t.append("\n\n")
        t.append("URL    ", style=PEACH); t.append(x.url, style=TEXT); t.append("\n")
        if x.expect or x.contains:
            t.append("WANT   ", style=PEACH)
            t.append(f"{'HTTP ' + str(x.expect) if x.expect else 'any 2xx/3xx'}"
                     f"{'  body contains ' + repr(x.contains) if x.contains else ''}", style=DIM)
            t.append("\n")
        t.append("\n")
        if r is None:
            t.append("checking…", style=DIM)
        else:
            age = int(time.time() - r.at)
            t.append("STATUS ", style=PEACH)
            if r.status:
                t.append(f"HTTP {r.status}", style=ORANGE if r.ok else RED)
            else:
                t.append("no response", style=RED)
            t.append(f"     RTT {r.latency_ms}ms", style=TEXT)
            t.append(f"   ({age}s ago)\n", style=DIM)
            if r.tls_days is not None:
                t.append("TLS    ", style=PEACH)
                t.append(f"expires in {r.tls_days} days", style=ORANGE if r.tls_days > 14 else AMBER if r.tls_days > 3 else RED)
                t.append("\n")
            if r.error:
                t.append("ERROR  ", style=PEACH); t.append(r.error, style=RED); t.append("\n")
        t.append("\n")
        t.append("enter", style=f"bold {ORANGE}"); t.append(" open in browser", style=DIM)
        panel.update(t)

    def render_detail(self) -> None:
        panel = self.query_one("#detail", Panel)
        site = self.selected_site
        if site is not None:
            self.render_site_detail(site)
            return
        u = self.selected
        if u is None:
            panel.update(Text("no unit selected", style=DIM))
            return
        peer = self.ts.peers.get(u.tailscale) if self.ts.ok else None
        probe = self.probes.get(u.name)
        glyph, colour = self.status_of(u)

        t = Text()
        t.append(f"{glyph} ", style=colour)
        t.append(u.name.upper(), style=f"bold {ORANGE}")
        t.append(f"   {u.kind}", style=PEACH)
        if u.tags:
            t.append("   " + "  ".join(f"#{x}" for x in u.tags), style=DIM)
        t.append("\n")
        if u.note:
            t.append(f"{u.note}\n", style=DIM)
        t.append("\n")

        # link
        t.append("LINK   ", style=PEACH)
        if peer is None:
            t.append("not in tailnet" if self.ts.ok else f"tailscale: {self.ts.error}", style=DIM)
        else:
            t.append(peer.dns or peer.hostname, style=TEXT)
            t.append(f"  {peer.ip}", style=DIM)
            t.append(f"  {peer.os}", style=DIM)
            if peer.online:
                t.append("  online", style=ORANGE)
                if peer.relay:
                    t.append(f"  via {peer.relay}", style=AMBER)
            else:
                t.append("  offline", style=RED)
                if peer.last_seen and not peer.last_seen.startswith("0001"):
                    t.append(f"  last {peer.last_seen[:16].replace('T', ' ')}", style=DIM)
        t.append("\n")
        t.append("SSH    ", style=PEACH)
        t.append(u.ssh, style=TEXT)
        if u.lan:
            t.append(f"   lan: {u.lan}", style=DIM)
        t.append("\n\n")

        # probe
        if probe is None:
            t.append("probing…" if u.name in self._probing else "no telemetry yet", style=DIM)
        elif not probe.ok:
            t.append("PROBE  ", style=PEACH)
            t.append(probe.error, style=RED)
        else:
            age = int(time.time() - probe.at)
            t.append("UP     ", style=PEACH)
            t.append(probe.uptime_h, style=TEXT)
            t.append("     LOAD ", style=PEACH)
            t.append(probe.load or "—", style=TEXT)
            t.append("     RTT ", style=PEACH)
            t.append(f"{probe.latency_ms}ms", style=TEXT)
            t.append(f"   ({age}s ago)", style=DIM)
            t.append("\n")
            t.append("MEM    ", style=PEACH)
            t.append_text(gauge(probe.mem_pct))
            t.append(f"  {probe.mem_used_kb / 1048576:.1f}G / {probe.mem_total_kb / 1048576:.1f}G", style=DIM)
            t.append("\n")
            t.append("DISK   ", style=PEACH)
            t.append_text(gauge(probe.disk_pct))
            t.append(f"  {probe.disk_used_kb / 1048576:.0f}G / {probe.disk_total_kb / 1048576:.0f}G on /", style=DIM)
            if probe.services:
                t.append("\n\n")
                t.append("SERVICES\n", style=PEACH)
                for name, state in probe.services.items():
                    if state == "active":
                        t.append(f"  {GLYPH_UP} ", style=ORANGE)
                        t.append(f"{name:<22}", style=TEXT)
                        t.append("active", style=ORANGE)
                    elif state in ("inactive", "unknown"):
                        t.append(f"  {GLYPH_DOWN} ", style=DIM)
                        t.append(f"{name:<22}", style=DIM)
                        t.append(state, style=DIM)
                    else:
                        t.append(f"  {GLYPH_HALF} ", style=RED)
                        t.append(f"{name:<22}", style=TEXT)
                        t.append(state, style=RED)
                    t.append("\n")
        linked = self.inv.sites_for(u)
        if linked:
            t.append("\n\n")
            t.append("SITES\n", style=PEACH)
            for s in linked:
                r = self.http.get(s.name)
                if r is None:
                    t.append(f"  {GLYPH_UNKNOWN} ", style=DIM); t.append(f"{s.name:<22}", style=DIM); t.append("pending", style=DIM)
                elif r.ok:
                    t.append(f"  {GLYPH_UP} ", style=ORANGE); t.append(f"{s.name:<22}", style=TEXT)
                    t.append(f"{r.status} · {r.latency_ms}ms", style=ORANGE)
                    if r.tls_days is not None:
                        t.append(f" · tls {r.tls_days}d", style=DIM if r.tls_days > 14 else AMBER)
                else:
                    t.append(f"  {GLYPH_DOWN} ", style=RED); t.append(f"{s.name:<22}", style=TEXT); t.append(r.error, style=RED)
                t.append("\n")
        panel.update(t)

    # ------------------------------------------------------------ pollers

    @work(exclusive=True, group="ts")
    async def poll_tailscale(self) -> None:
        ts = await tailscale_status()
        self.apply_tailscale(ts)

    def apply_tailscale(self, ts: TSStatus) -> None:
        was_ok = self.ts.ok
        prev = {k: p.online for k, p in self.ts.peers.items()} if self.ts.ok else {}
        self.ts = ts
        line = self.query_one("#tsline", Static)
        if ts.ok:
            online = sum(1 for u in self.inv.units if (p := ts.peers.get(u.tailscale)) and p.online)
            line.update(Text.assemble(("TAILNET ", PEACH), (f"{online}", f"bold {ORANGE}"),
                                      (f"/{len(self.inv.units)} online", TEXT),
                                      (f"   as {ts.self_name}", DIM), ("   ", ""),
                                      (self.inv.squadron, f"bold {ORANGE}")))
            if not was_ok:
                self.log_line("tailscale", f"link up · {online} units online", "ok")
            for u in self.inv.units:
                p = ts.peers.get(u.tailscale)
                if p is None:
                    continue
                before = prev.get(u.tailscale)
                if before is not None and before != p.online:
                    self.log_line("tailscale", f"{u.name} {'came online' if p.online else 'went offline'}",
                                  "ok" if p.online else "warn")
                    if p.online:
                        self.probe_unit(u)
        else:
            line.update(Text.assemble(("TAILNET ", PEACH), ("down", f"bold {RED}"),
                                      (f"  {ts.error}", DIM)))
            if was_ok:
                self.log_line("tailscale", ts.error, "err")
        for u in self.inv.units:
            self.refresh_row(u)
        self.render_detail()

    def _via(self, u: Unit) -> tuple[str, str]:
        host = self.inv.by_name(u.via)
        return (host.ssh if host else u.via, u.via_exec)

    def _via_identity(self, u: Unit) -> str:
        host = self.inv.by_name(u.via)
        return self.inv.identity_for(host) if host else self.inv.identity

    def probe_all(self) -> None:
        for u in self.inv.units:
            peer = self.ts.peers.get(u.tailscale) if self.ts.ok else None
            if peer is not None and not peer.online and not u.lan:
                continue  # don't waste a timeout on a node tailscale says is down
            self.probe_unit(u)

    @work(group="probe")
    async def probe_unit(self, u: Unit) -> None:
        if u.name in self._probing:
            return
        self._probing.add(u.name)
        self.refresh_row(u)
        try:
            ident = self.inv.identity_for(u)
            # direct ssh first (root@<name> for containers), lan alias next,
            # host exec last — and remember which path worked for Enter
            p = await ssh_probe(u, identity=ident)
            self.direct_ok[u.name] = p.ok
            if not p.ok and u.lan:
                peer = self.ts.peers.get(u.tailscale) if self.ts.ok else None
                if peer is None or not peer.online:
                    p = await ssh_probe(u, target=u.lan, identity=ident)
            if not p.ok and u.via:
                p = await ssh_probe(u, identity=self._via_identity(u), via=self._via(u))
        finally:
            self._probing.discard(u.name)
        old = self.probes.get(u.name)
        self.probes[u.name] = p
        if old is None or old.ok != p.ok:
            if p.ok:
                self.log_line("probe", f"{u.name} responding · {p.latency_ms}ms · up {p.uptime_h}", "ok")
            else:
                self.log_line("probe", f"{u.name} unreachable · {p.error}", "warn")
        elif p.ok and old.ok:
            for svc, state in p.services.items():
                if old.services.get(svc) != state:
                    self.log_line("service", f"{u.name}/{svc} → {state}",
                                  "ok" if state == "active" else "err")
        self.refresh_row(u)
        if self.selected and self.selected.name == u.name:
            self.render_detail()

    # ------------------------------------------------------------ sites

    def site_cells(self, x: Site, last: bool = False, orphan: bool = False) -> list[Text]:
        """A tree child row under its unit:  └─ blog   http  200  240ms  75d"""
        r = self.http.get(x.name)
        if r is None:
            glyph, colour = GLYPH_UNKNOWN, DIM
        elif r.ok:
            glyph, colour = GLYPH_UP, ORANGE
        else:
            glyph, colour = GLYPH_DOWN, RED
        branch = ("  " if orphan else "") + ("└─ " if last else "├─ ")
        name = Text(branch, style=DIM)
        name.append(x.name, style=TEXT if (r and r.ok) else DIM)
        tag = Text("https" if x.url.startswith("https") else "http", style=DIM)
        link = Text("—", style=DIM) if r is None else \
            Text(str(r.status) if r.status else r.error[:14], style=ORANGE if r.ok else RED)
        rtt = Text("—", style=DIM) if r is None or not r.status else \
            Text(f"{r.latency_ms}ms", style=ORANGE if r.latency_ms < 800 else AMBER)
        if r is None or r.tls_days is None:
            tls = Text("—", style=DIM)
        else:
            tls = Text(f"{r.tls_days}d", style=ORANGE if r.tls_days > 14 else AMBER if r.tls_days > 3 else RED)
        return [Text(glyph, style=colour), name, tag, link, rtt, tls]

    def refresh_site_row(self, x: Site) -> None:
        table = self.query_one("#units", DataTable)
        key = f"site:{x.name}"
        try:
            table.get_row_index(key)
        except Exception:  # noqa: BLE001 — filtered out
            return
        last = table.get_cell(key, "name").plain.startswith(("└─", "  └─"))
        for col, cell in zip(("st", "name", "tag", "link", "rtt", "svc"),
                             self.site_cells(x, last=last, orphan=not x.unit)):
            table.update_cell(key, col, cell)

    def poll_sites(self) -> None:
        for x in self.inv.all_sites():
            self.check_site(x)

    @work(group="http")
    async def check_site(self, s: Site) -> None:
        r = await http_check(s)
        old = self.http.get(s.name)
        self.http[s.name] = r
        if old is None or old.ok != r.ok:
            if r.ok:
                self.log_line("http", f"{s.name} up · {r.status} · {r.latency_ms}ms", "ok")
            else:
                self.log_line("http", f"{s.name} DOWN · {r.error}", "err")
        if r.tls_days is not None and r.tls_days <= 14 and (old is None or old.tls_days != r.tls_days):
            self.log_line("http", f"{s.name} TLS cert expires in {r.tls_days}d", "warn")
        self.refresh_site_row(s)
        if s.unit:
            parent = self.inv.by_name(s.unit)
            if parent:
                self.refresh_row(parent)
        sel = self.selected_site
        if (sel and sel.name == s.name) or (s.unit and self.selected and self.selected.name == s.unit):
            self.render_detail()

    @work(exclusive=True, group="pve")
    async def poll_proxmox(self) -> None:
        assert self.inv.proxmox
        st = await proxmox_status(self.inv.proxmox)
        prev = self.pve
        self.pve = st
        table = self.query_one("#guests", DataTable)
        if not st.ok:
            if prev is None or prev.ok:
                self.log_line("proxmox", st.error, "err")
            table.border_title = "PROXMOX GUESTS · offline"
            return
        running = sum(1 for g in st.guests if g.status == "running")
        table.border_title = f"PROXMOX GUESTS · {running}/{len(st.guests)} running"
        if prev is None or not prev.ok:
            self.log_line("proxmox", f"{len(st.guests)} guests · {running} running", "ok")
        elif prev.ok:
            before = {g.vmid: g.status for g in prev.guests}
            for g in st.guests:
                if before.get(g.vmid) not in (None, g.status):
                    self.log_line("proxmox", f"{g.name} ({g.vmid}) → {g.status}",
                                  "ok" if g.status == "running" else "warn")
        table.clear()
        for g in st.guests:
            up = g.status == "running"
            table.add_row(
                Text(GLYPH_UP if up else GLYPH_DOWN, style=ORANGE if up else DIM),
                Text(str(g.vmid), style=DIM),
                Text(g.name, style=TEXT if up else DIM),
                Text("VM" if g.kind == "qemu" else "CT", style=PEACH),
                Text(g.node, style=DIM),
                Text(f"{g.cpu * 100:3.0f}%" if up else "—", style=TEXT if up else DIM),
                Text(f"{g.mem / 2**30:.1f}/{g.maxmem / 2**30:.0f}G" if up else "—", style=TEXT if up else DIM),
                key=str(g.vmid),
            )

    # ------------------------------------------------------------ actions

    def _ssh(self, target: str, label: str, identity: str = "") -> None:
        self.log_line("ssh", f"→ {label}", "ok")
        with self.suspend():
            print(f"\x1b[38;2;255;138;0m▌FENNIA▐ connecting to {label} …\x1b[0m")
            rc = subprocess.call(["ssh", *ssh_identity_args(identity), target])
        self.log_line("ssh", f"← {label} (exit {rc})", "ok" if rc == 0 else "warn")
        u = self.inv.by_name(label.split()[0])
        if u:
            self.probe_unit(u)

    def action_connect(self) -> None:
        site = self.selected_site
        if site is not None:
            import webbrowser
            self.log_line("http", f"open {site.url}", "ok")
            webbrowser.open(site.url)
            return
        u = self.selected
        if u is None:
            return
        # plain ssh unless we know it doesn't work and there's a host to go through
        if u.via and self.direct_ok.get(u.name) is False:
            self.action_connect_via()
        else:
            self._ssh(u.ssh, u.name, self.inv.identity_for(u))

    def action_connect_via(self) -> None:
        """Shell into a container through its host (incus exec / pct exec)."""
        u = self.selected
        if u is None or not u.via:
            self.notify("unit has no via: host", severity="warning", title="via")
            return
        host, prefix = self._via(u)
        label = f"{u.name} (via {u.via})"
        self.log_line("ssh", f"→ {label}", "ok")
        with self.suspend():
            print(f"\x1b[38;2;255;138;0m▌FENNIA▐ {prefix} on {host} …\x1b[0m")
            fn = 'mctS() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo -H "$@"; fi; }; '
            rc = subprocess.call(["ssh", "-t", *ssh_identity_args(self._via_identity(u)), host,
                                  f"{fn}mctS {prefix} sh -c 'exec bash || exec sh'"])
        self.log_line("ssh", f"← {label} (exit {rc})", "ok" if rc == 0 else "warn")
        self.probe_unit(u)

    def action_connect_lan(self) -> None:
        u = self.selected
        if u is None:
            return
        if not u.lan:
            self.notify(f"{u.name} has no LAN alias", severity="warning", title="no lan route")
            return
        self._ssh(u.lan, f"{u.name} (lan)", self.inv.identity_for(u))

    def action_refresh(self) -> None:
        self.log_line("mct", "manual refresh")
        self.poll_tailscale()
        self.probe_all()
        if self.inv.all_sites():
            self.poll_sites()
        if self.inv.proxmox:
            self.poll_proxmox()

    def action_tailscale_ping(self) -> None:
        u = self.selected
        if u is None:
            return
        self.run_ts_ping(u)

    @work(group="tsping")
    async def run_ts_ping(self, u: Unit) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "tailscale", "ping", "-c", "1", u.tailscale,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            self.log_line("ts-ping", out.decode(errors="ignore").strip().splitlines()[-1], "info")
        except Exception as exc:  # noqa: BLE001
            self.log_line("ts-ping", str(exc), "err")

    # ------------------------------------------------------------ add / edit

    @on(Button.Pressed, "#btn-add")
    def _btn_add(self) -> None:
        self.action_add_unit()

    @on(Button.Pressed, "#btn-edit")
    def _btn_edit(self) -> None:
        self.action_edit_unit()

    def action_add_unit(self) -> None:
        self.push_screen(UnitForm(None, {u.name for u in self.inv.units}),
                         callback=lambda r: self._form_done(r, None))

    def action_edit_unit(self) -> None:
        u = self.selected
        if u is None:
            self.notify("no unit highlighted", severity="warning", title="edit")
            return
        self.push_screen(UnitForm(u, {x.name for x in self.inv.units}),
                         callback=lambda r: self._form_done(r, u.name))

    def _form_done(self, result: FormResult, editing: str | None) -> None:
        self.query_one("#units", DataTable).focus()
        if result is None:
            return
        action, payload = result
        if action == "remove":
            assert isinstance(payload, str)
            self.inv.remove(payload)
            self.probes.pop(payload, None)
            self.log_line("units", f"removed {payload}", "warn")
        else:
            assert isinstance(payload, Unit)
            self.inv.upsert(payload, replace=editing)
            if editing and editing != payload.name:
                self.probes.pop(editing, None)
            self.log_line("units", f"{'updated' if editing else 'added'} {payload.name}", "ok")
        try:
            path = save_inventory(self.inv)
        except OSError as exc:
            self.log_line("units", f"save failed: {exc}", "err")
            self.notify(str(exc), severity="error", title="inventory not saved")
        else:
            self.log_line("units", f"saved {pretty_path(path)}", "info")
        self.rebuild_table()
        if action == "save":
            table = self.query_one("#units", DataTable)
            try:
                table.move_cursor(row=table.get_row_index(payload.name))
            except Exception:  # noqa: BLE001 — filtered out
                pass
            self.render_detail()
            self.probe_unit(payload)
        self.apply_tailscale(self.ts)

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.display = True
        box.focus()

    def action_clear_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.value = ""
        box.display = False
        self.filter_text = ""
        self.rebuild_table()
        self.query_one("#units", DataTable).focus()

    @on(Input.Changed, "#filter")
    def _filter_changed(self, ev: Input.Changed) -> None:
        self.filter_text = ev.value
        self.rebuild_table()

    @on(Input.Submitted, "#filter")
    def _filter_submit(self) -> None:
        self.query_one("#units", DataTable).focus()


def ssh_passthrough(argv: list[str]) -> int:
    """mct ssh <unit> [command…] — plain ssh with the unit's alias + identity.
    --lan uses the lan: alias; --via runs through the host's exec instead."""
    lan = "--lan" in argv
    via = "--via" in argv
    rest = [a for a in argv if a not in ("--lan", "--via")]
    if not rest or rest[0] in ("-h", "--help"):
        print("usage: mct ssh [--lan|--via] <unit> [command…]", file=sys.stderr)
        return 2
    inv = load_inventory()
    u = inv.by_name(rest[0])
    if u is None:
        print(f"mct ssh: no unit {rest[0]!r} in {pretty_path(inv.source)}", file=sys.stderr)
        return 1
    cmd = rest[1:]
    if via:
        host = inv.by_name(u.via)
        target = host.ssh if host else u.via
        ident = inv.identity_for(host) if host else inv.identity
        inner = shlex.join(cmd) if cmd else "sh -c 'exec bash || exec sh'"
        fn = 'mctS() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo -H "$@"; fi; }; '
        argv2 = ["ssh", "-t", *ssh_identity_args(ident), target, f"{fn}mctS {u.via_exec} {inner}"]
    else:
        target = u.lan if lan and u.lan else u.ssh
        # -t so sudo/passwd prompts work with a command; harmless interactively
        argv2 = ["ssh", "-t", *ssh_identity_args(inv.identity_for(u)), target, *cmd]
    return subprocess.call(argv2)


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["keys"]:
        from .keys import cli
        sys.exit(cli(argv[1:]))
    if argv[:1] == ["enroll"]:
        from .enroll import cli as enroll_cli
        sys.exit(enroll_cli(argv[1:]))
    if argv[:1] == ["ssh"]:
        sys.exit(ssh_passthrough(argv[1:]))
    ap = argparse.ArgumentParser(prog="mct", description="FENNIA Master Control Terminal",
                                 epilog="subcommands:  mct ssh <unit> [cmd…]   mct keys gen|add|list|path   mct enroll [units…]")
    ap.add_argument("--no-boot", action="store_true", help="skip the boot sequence")
    ap.add_argument("-i", "--inventory", help="path to inventory.yaml")
    args = ap.parse_args(argv)
    if args.inventory and not pathlib.Path(args.inventory).expanduser().is_file():
        print(f"mct: inventory not found: {args.inventory}", file=sys.stderr)
        sys.exit(1)
    inv = load_inventory(args.inventory)
    if not inv.units:
        print("mct: no units found — write ~/.config/mct/inventory.yaml (see inventory.example.yaml) "
              "or add Host entries to ~/.ssh/config", file=sys.stderr)
        sys.exit(1)
    MCT(inv, boot=not args.no_boot).run()


if __name__ == "__main__":
    main()
