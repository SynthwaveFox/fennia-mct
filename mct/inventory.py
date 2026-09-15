"""Inventory: the units under Fennia command.

Loaded from inventory.yaml (see the example at the repo root). If no file is
found we fall back to scanning ~/.ssh/config so the selector still works.
"""
from __future__ import annotations

import os
import re
import sys
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
class Site:
    name: str
    url: str
    expect: int = 0                # exact status wanted; 0 = any 2xx/3xx
    contains: str = ""             # body must contain this text
    unit: str = ""                 # set automatically from the parent unit
    timeout: float = 8.0


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
    via: str = ""                  # unit name of the host that runs this container (incus / pct)
    via_exec: str = ""             # command prefix on that host, e.g. "incus exec media --"
    sites: list = field(default_factory=list)   # list[Site] — HTTP checks this unit serves

    def __post_init__(self) -> None:
        self.sites = [s if isinstance(s, Site) else Site(**s) for s in self.sites]
        self._ssh_explicit = bool(self.ssh)
        # exec through a host runs as root, so that's the account the key lands in
        self.ssh = self.ssh or (f"root@{self.name}" if self.via else self.name)
        self.tailscale = (self.tailscale or self.name).lower()
        if self.via and not self.via_exec:
            self.via_exec = f"incus exec {self.name} --"

    @property
    def direct_ssh(self) -> bool:
        """Reach it with plain ssh (true unless it's only reachable via its host)."""
        return not self.via or self._ssh_explicit


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
    sites: list[Site] = field(default_factory=list)   # sites with no unit (checked, listed at the end)
    http_interval: int = 60        # seconds between site checks
    source: str = ""
    path: Path = DEFAULT_PATH      # where save_inventory() writes

    def identity_for(self, unit: Unit) -> str:
        return unit.identity or self.identity

    def by_name(self, name: str) -> Unit | None:
        return next((u for u in self.units if u.name == name), None)

    def sites_for(self, unit: Unit) -> list[Site]:
        return unit.sites

    def all_sites(self) -> list[Site]:
        return [s for u in self.units for s in u.sites] + list(self.sites)

    def site_by_name(self, name: str) -> Site | None:
        return next((s for s in self.all_sites() if s.name == name), None)

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
            units: list[Unit] = []
            seen: set[str] = set()
            for raw in data.get("units", []):
                u = Unit(**raw)
                if u.name in seen:
                    print(f"mct: duplicate unit {u.name!r} in {path} — keeping the first", file=sys.stderr)
                    continue
                seen.add(u.name)
                units.append(u)
            pve = Proxmox(**data["proxmox"]) if data.get("proxmox") else None
            by_name = {u.name: u for u in units}
            for u in units:
                for s in u.sites:
                    s.unit = u.name
            sites: list[Site] = []
            for raw in data.get("sites", []):            # top-level: attach if unit: names one
                s = Site(**raw)
                if s.unit and s.unit in by_name:
                    by_name[s.unit].sites.append(s)
                else:
                    s.unit = ""
                    sites.append(s)
            return Inventory(
                callsign=data.get("callsign", "Commander"),
                squadron=data.get("squadron", "FX-2 SQUADRON"),
                units=units,
                proxmox=pve,
                probe_interval=int(data.get("probe_interval", 30)),
                tailscale_interval=int(data.get("tailscale_interval", 5)),
                identity=str(data.get("identity", "") or ""),
                sites=sites,
                http_interval=int(data.get("http_interval", 60)),
                source=str(path),
                path=path,
            )
    return Inventory(units=_from_ssh_config(), source="~/.ssh/config")


def _unit_dict(u: Unit) -> dict:
    """Compact YAML form: drop defaults so the file stays hand-editable."""
    d = asdict(u)
    if d["ssh"] == u.name or (u.via and d["ssh"] == f"root@{u.name}"):
        d.pop("ssh")
    if d["tailscale"] == u.name.lower():
        d.pop("tailscale")
    for k in ("lan", "note", "identity", "via", "via_exec"):
        if not d[k]:
            d.pop(k)
    if u.via and d.get("via_exec") == f"incus exec {u.name} --":
        d.pop("via_exec")
    d.pop("_ssh_explicit", None)
    for k in ("tags", "services"):
        if not d[k]:
            d.pop(k)
    if d["kind"] == "host":
        d.pop("kind")
    if u.sites:
        d["sites"] = [_site_dict(s) for s in u.sites]
    else:
        d.pop("sites", None)
    return d


def _site_dict(s: Site) -> dict:
    d = {"name": s.name, "url": s.url}
    if s.expect:
        d["expect"] = s.expect
    if s.contains:
        d["contains"] = s.contains
    if s.timeout != 8.0:
        d["timeout"] = s.timeout
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
    if inv.all_sites():
        data["http_interval"] = inv.http_interval
    if inv.sites:
        data["sites"] = [_site_dict(s) for s in inv.sites]
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
