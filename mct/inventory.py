"""Inventory: the units under Fennia command.

Loaded from inventory.yaml (see the example at the repo root). If no file is
found we fall back to scanning ~/.ssh/config so the selector still works.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

DEFAULT_PATH = Path.home() / ".config" / "mct" / "inventory.yaml"


def search_paths(explicit: str | Path | None = None) -> list[Path]:
    """Candidate inventory files, most specific first. Evaluated lazily so
    -i / $MCT_INVENTORY set after import still count."""
    paths: list[Path] = []
    if explicit:
        paths.append(Path(explicit).expanduser())
    if os.environ.get("MCT_INVENTORY"):
        paths.append(Path(os.environ["MCT_INVENTORY"]).expanduser())
    paths += [Path.cwd() / "inventory.yaml", DEFAULT_PATH, Path.home() / ".mct.yaml"]
    return paths
KINDS = ("host", "pve", "vm", "lxc", "appliance")


@dataclass
class Unit:
    name: str                      # display name + default ssh alias
    ssh: str = ""                  # ssh alias/host (defaults to name)
    lan: str = ""                  # optional LAN-side alias for fallback
    tailscale: str = ""            # tailscale hostname (defaults to name)
    tags: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)   # systemd units to probe
    kind: str = "host"             # host | pve | lxc | vm | appliance
    note: str = ""
    identity: str = ""             # key name in ~/.config/mct/keys, or a path; "" = inventory default

    def __post_init__(self) -> None:
        self.ssh = self.ssh or self.name
        self.tailscale = (self.tailscale or self.name).lower()


@dataclass
class Proxmox:
    host: str                      # https://pve01:8006
    user: str                      # root@pam
    token_name: str                # mct
    token_value: str = ""          # or set PVE_TOKEN env var
    verify_tls: bool = False

    @property
    def token(self) -> str:
        return self.token_value or os.environ.get("PVE_TOKEN", "")


@dataclass
class Inventory:
    callsign: str = "Commander"
    squadron: str = "FX-2 SQUADRON"
    units: list[Unit] = field(default_factory=list)
    proxmox: Proxmox | None = None
    probe_interval: int = 30       # seconds between ssh probes
    tailscale_interval: int = 5    # seconds between tailscale status polls
    identity: str = ""             # default key for every unit ("" = let ssh decide)
    source: str = ""
    path: Path = DEFAULT_PATH      # where save_inventory() writes

    def identity_for(self, unit: Unit) -> str:
        return unit.identity or self.identity

    def by_name(self, name: str) -> Unit | None:
        return next((u for u in self.units if u.name == name), None)

    def upsert(self, unit: Unit, replace: str | None = None) -> None:
        """Add `unit`, or replace the unit currently named `replace`."""
        if replace is not None:
            for i, u in enumerate(self.units):
                if u.name == replace:
                    self.units[i] = unit
                    return
        self.units.append(unit)

    def remove(self, name: str) -> None:
        self.units = [u for u in self.units if u.name != name]


def pretty_path(path: str | Path) -> str:
    """~/.config/mct/inventory.yaml instead of the full home prefix."""
    p = str(path)
    home = str(Path.home())
    return "~" + p[len(home):].replace("\\", "/") if p.startswith(home) else p


def _from_ssh_config() -> list[Unit]:
    cfg = Path.home() / ".ssh" / "config"
    if not cfg.exists():
        return []
    units: list[Unit] = []
    for line in cfg.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(r"^\s*Host\s+(.+)$", line)
        if not m:
            continue
        for alias in m.group(1).split():
            if "*" in alias or "?" in alias:
                continue
            units.append(Unit(name=alias, tags=["ssh-config"]))
    return units


def load_inventory(explicit: str | Path | None = None) -> Inventory:
    for path in search_paths(explicit):
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            units = [Unit(**u) for u in data.get("units", [])]
            pve = Proxmox(**data["proxmox"]) if data.get("proxmox") else None
            return Inventory(
                callsign=data.get("callsign", "Commander"),
                squadron=data.get("squadron", "FX-2 SQUADRON"),
                units=units,
                proxmox=pve,
                probe_interval=int(data.get("probe_interval", 30)),
                tailscale_interval=int(data.get("tailscale_interval", 5)),
                identity=str(data.get("identity", "") or ""),
                source=str(path),
                path=path,
            )
    return Inventory(units=_from_ssh_config(), source="~/.ssh/config")


def _unit_dict(u: Unit) -> dict:
    """Compact YAML form: drop defaults so the file stays hand-editable."""
    d = asdict(u)
    if d["ssh"] == u.name:
        d.pop("ssh")
    if d["tailscale"] == u.name.lower():
        d.pop("tailscale")
    for k in ("lan", "note", "identity"):
        if not d[k]:
            d.pop(k)
    for k in ("tags", "services"):
        if not d[k]:
            d.pop(k)
    if d["kind"] == "host":
        d.pop("kind")
    return d


def save_inventory(inv: Inventory) -> Path:
    """Write the inventory back to its YAML. Comments in the file are not kept."""
    data: dict = {
        "callsign": inv.callsign,
        "squadron": inv.squadron,
        "probe_interval": inv.probe_interval,
        "tailscale_interval": inv.tailscale_interval,
    }
    if inv.identity:
        data["identity"] = inv.identity
    if inv.proxmox:
        pve = asdict(inv.proxmox)
        if not pve["token_value"]:
            pve.pop("token_value")
        data["proxmox"] = pve
    data["units"] = [_unit_dict(u) for u in inv.units]
    inv.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = inv.path.with_suffix(".yaml.tmp")
    tmp.write_text(
        "# FENNIA MCT inventory — managed by mct (edits in-app are saved here)\n"
        + yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    tmp.replace(inv.path)
    inv.source = str(inv.path)
    return inv.path
