"""Unit add / edit modal.

Returns one of:
    ("save", Unit)     — add or replace
    ("remove", name)   — delete the unit being edited
    None               — cancelled
"""
from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from .inventory import KINDS, Unit
from .theme import DIM, ORANGE, PEACH

FormResult = tuple[str, Unit | str] | None


def _csv(value: str) -> list[str]:
    return [x.strip() for x in value.replace(";", ",").split(",") if x.strip()]


class UnitForm(ModalScreen[FormResult]):
    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("ctrl+s", "save", "save"),
    ]

    def __init__(self, unit: Unit | None, taken: set[str]) -> None:
        super().__init__()
        self.unit = unit
        self.taken = taken - ({unit.name} if unit else set())

    # ------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        u = self.unit
        editing = u is not None
        with Vertical(id="form"):
            yield Static(
                Text.assemble(("▌", ORANGE), ("EDIT UNIT" if editing else "NEW UNIT", f"bold {ORANGE}"),
                              ("▐ ", ORANGE), (u.name if editing else "", PEACH)),
                id="form-title",
            )
            with Grid(id="form-grid"):
                yield Label("name")
                yield Input(u.name if u else "", placeholder="pve01  (also the ssh alias)", id="f-name")
                yield Label("kind")
                yield Select(((k, k) for k in KINDS), value=u.kind if u else "host",
                             allow_blank=False, id="f-kind")
                yield Label("ssh")
                yield Input(u.ssh if u and u.ssh != u.name else "",
                            placeholder="alias from ~/.ssh/config — blank = name", id="f-ssh")
                yield Label("lan")
                yield Input(u.lan if u else "", placeholder="pve01-lan  (fallback alias, optional)", id="f-lan")
                yield Label("tailscale")
                yield Input(u.tailscale if u and u.tailscale != u.name.lower() else "",
                            placeholder="tailnet hostname — blank = name", id="f-ts")
                yield Label("tags")
                yield Input(", ".join(u.tags) if u else "", placeholder="core, net, lab", id="f-tags")
                yield Label("services")
                yield Input(", ".join(u.services) if u else "",
                            placeholder="pveproxy, docker, pihole-FTL  (systemd units)", id="f-svc")
                yield Label("via")
                yield Input(u.via if u else "", placeholder="host unit that runs this container (truenas, pve01)", id="f-via")
                yield Label("via exec")
                yield Input(u.via_exec if u and u.via else "",
                            placeholder="incus exec <name> --   |   pct exec <vmid> --   (blank = incus)", id="f-viaexec")
                yield Label("identity")
                yield Input(u.identity if u else "", placeholder="key name in ~/.config/mct/keys — blank = default", id="f-ident")
                yield Label("note")
                yield Input(u.note if u else "", placeholder="free text", id="f-note")
            yield Static("", id="form-error")
            with Horizontal(id="form-buttons"):
                yield Button("SAVE", id="save", variant="primary")
                yield Button("CANCEL", id="cancel")
                if editing:
                    yield Static("", id="form-spacer")
                    yield Button("REMOVE", id="remove", variant="error")
            yield Static(Text("enter/ctrl+s save   esc cancel   tab next field", style=DIM), id="form-hint")

    def on_mount(self) -> None:
        self.query_one("#f-name", Input).focus()

    # ------------------------------------------------------------ actions

    def _error(self, msg: str) -> None:
        self.query_one("#form-error", Static).update(Text(f"✕ {msg}", style="bold #f38ba8"))

    def _build(self) -> Unit | None:
        name = self.query_one("#f-name", Input).value.strip()
        if not name:
            self._error("name is required")
            self.query_one("#f-name", Input).focus()
            return None
        if any(c.isspace() for c in name):
            self._error("name can't contain spaces (it's the ssh alias)")
            return None
        if name in self.taken:
            self._error(f"a unit named {name} already exists")
            return None
        kind = self.query_one("#f-kind", Select).value
        return Unit(
            name=name,
            ssh=self.query_one("#f-ssh", Input).value.strip(),
            lan=self.query_one("#f-lan", Input).value.strip(),
            tailscale=self.query_one("#f-ts", Input).value.strip(),
            tags=_csv(self.query_one("#f-tags", Input).value),
            services=_csv(self.query_one("#f-svc", Input).value),
            kind=str(kind) if kind is not Select.BLANK else "host",
            note=self.query_one("#f-note", Input).value.strip(),
            identity=self.query_one("#f-ident", Input).value.strip(),
            via=self.query_one("#f-via", Input).value.strip(),
            via_exec=self.query_one("#f-viaexec", Input).value.strip(),
        )

    def action_save(self) -> None:
        unit = self._build()
        if unit is not None:
            self.dismiss(("save", unit))

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#remove")
    def _remove(self) -> None:
        assert self.unit is not None
        self.dismiss(("remove", self.unit.name))

    @on(Input.Submitted)
    def _submit(self) -> None:
        self.action_save()
