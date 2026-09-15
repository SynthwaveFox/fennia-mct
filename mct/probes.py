"""Telemetry sources: Tailscale, SSH probe, Proxmox API.

Everything here is async and never raises — failures come back as a result
object with `ok=False` so the UI can paint it dim instead of crashing.
"""
from __future__ import annotations

import asyncio
import json
import shlex
import ssl
import time
import urllib.request
from dataclasses import dataclass, field

from .inventory import Proxmox, Unit
from .keys import ssh_identity_args

# ----------------------------------------------------------------- tailscale


@dataclass
class TSPeer:
    hostname: str
    dns: str
    ip: str
    online: bool
    os: str
    relay: str
    last_seen: str
    exit_node: bool = False


@dataclass
class TSStatus:
    ok: bool
    peers: dict[str, TSPeer] = field(default_factory=dict)   # keyed by lowercase hostname
    self_name: str = ""
    error: str = ""


async def tailscale_status() -> TSStatus:
    try:
        proc = await asyncio.create_subprocess_exec(
            "tailscale", "status", "--json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=6)
    except FileNotFoundError:
        return TSStatus(ok=False, error="tailscale CLI not found")
    except asyncio.TimeoutError:
        return TSStatus(ok=False, error="tailscale status timed out")
    except Exception as exc:  # noqa: BLE001
        return TSStatus(ok=False, error=str(exc))
    if proc.returncode != 0:
        return TSStatus(ok=False, error=err.decode(errors="ignore").strip() or "tailscale error")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return TSStatus(ok=False, error="bad json from tailscale")

    peers: dict[str, TSPeer] = {}
    nodes = list((data.get("Peer") or {}).values())
    if data.get("Self"):
        nodes.append(data["Self"])
    for n in nodes:
        host = (n.get("HostName") or "").lower()
        dns = (n.get("DNSName") or "").rstrip(".")
        ips = n.get("TailscaleIPs") or []
        peer = TSPeer(
            hostname=host,
            dns=dns,
            ip=ips[0] if ips else "",
            online=bool(n.get("Online")),
            os=n.get("OS") or "",
            relay=n.get("Relay") or "",
            last_seen=n.get("LastSeen") or "",
            exit_node=bool(n.get("ExitNode")),
        )
        peers[host] = peer
        if dns:
            peers.setdefault(dns.split(".")[0].lower(), peer)
    return TSStatus(ok=True, peers=peers, self_name=(data.get("Self") or {}).get("HostName", ""))


# ----------------------------------------------------------------- ssh probe

# POSIX sh; works on Debian/Arch/Alpine LXCs alike. Piped to `sh -s` on the remote.
REMOTE_SCRIPT = r"""
printf 'HOST=%s\n' "$(hostname 2>/dev/null)"
printf 'UP=%s\n' "$(cut -d. -f1 /proc/uptime 2>/dev/null)"
printf 'LOAD=%s\n' "$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)"
awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{printf "MEM=%d %d\n", t-a, t}' /proc/meminfo 2>/dev/null
df -P / 2>/dev/null | awk 'NR==2{printf "DISK=%s %s\n", $3, $2}'
for s in {services}; do
  printf 'SVC=%s %s\n' "$s" "$(systemctl is-active "$s" 2>/dev/null || echo unknown)"
done
"""


@dataclass
class Probe:
    ok: bool
    latency_ms: int = 0
    hostname: str = ""
    uptime_s: int = 0
    load: str = ""
    mem_used_kb: int = 0
    mem_total_kb: int = 0
    disk_used_kb: int = 0
    disk_total_kb: int = 0
    services: dict[str, str] = field(default_factory=dict)
    error: str = ""
    at: float = 0.0

    @property
    def mem_pct(self) -> float:
        return (self.mem_used_kb / self.mem_total_kb * 100) if self.mem_total_kb else 0.0

    @property
    def disk_pct(self) -> float:
        return (self.disk_used_kb / self.disk_total_kb * 100) if self.disk_total_kb else 0.0

    @property
    def uptime_h(self) -> str:
        s = self.uptime_s
        d, s = divmod(s, 86400)
        h, s = divmod(s, 3600)
        m = s // 60
        if d:
            return f"{d}d {h}h"
        if h:
            return f"{h}h {m}m"
        return f"{m}m"


def _base_ssh(timeout: float, identity: str) -> list[str]:
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={int(timeout // 2) or 1}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "LogLevel=ERROR",
        *ssh_identity_args(identity),
    ]


async def ssh_probe(unit: Unit, target: str | None = None, timeout: float = 8.0,
                    identity: str = "", via: tuple[str, str] | None = None) -> Probe:
    """Run REMOTE_SCRIPT over ssh. `target` overrides the ssh alias (LAN fallback).
    `via` = (host target, exec prefix) runs the script inside a container through
    its host, e.g. ("truenas", "incus exec media --")."""
    script = REMOTE_SCRIPT.replace("{services}", " ".join(shlex.quote(s) for s in unit.services))
    if via:
        host, prefix = via
        # shell function, not $VAR: zsh (TrueNAS admin) doesn't word-split variables.
        # -n: never hang on a sudo prompt
        fn = 'mctS() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo -n -H "$@"; fi; }; '
        argv = [*_base_ssh(timeout, identity), host, f"{fn}mctS {prefix} sh -s"]
    else:
        argv = [*_base_ssh(timeout, identity), target or unit.ssh, "sh", "-s"]
    t0 = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(script.encode()), timeout=timeout)
    except FileNotFoundError:
        return Probe(ok=False, error="ssh not found", at=time.time())
    except asyncio.TimeoutError:
        return Probe(ok=False, error="timeout", at=time.time())
    except Exception as exc:  # noqa: BLE001
        return Probe(ok=False, error=str(exc), at=time.time())
    ms = int((time.perf_counter() - t0) * 1000)
    if proc.returncode != 0:
        msg = err.decode(errors="ignore").strip().splitlines()
        return Probe(ok=False, latency_ms=ms, error=(msg[-1] if msg else f"exit {proc.returncode}"), at=time.time())

    p = Probe(ok=True, latency_ms=ms, at=time.time())
    for line in out.decode(errors="ignore").splitlines():
        key, _, val = line.partition("=")
        val = val.strip()
        try:
            if key == "HOST":
                p.hostname = val
            elif key == "UP" and val:
                p.uptime_s = int(val)
            elif key == "LOAD":
                p.load = val
            elif key == "MEM":
                a, b = val.split()
                p.mem_used_kb, p.mem_total_kb = int(a), int(b)
            elif key == "DISK":
                a, b = val.split()
                p.disk_used_kb, p.disk_total_kb = int(a), int(b)
            elif key == "SVC":
                name, _, state = val.partition(" ")
                p.services[name] = state.strip() or "unknown"
        except ValueError:
            continue
    return p


# ----------------------------------------------------------------- proxmox


@dataclass
class Guest:
    vmid: int
    name: str
    kind: str        # qemu | lxc
    status: str      # running | stopped
    node: str
    cpu: float       # 0..1 fraction of maxcpu
    mem: int
    maxmem: int
    uptime: int


@dataclass
class PVEStatus:
    ok: bool
    guests: list[Guest] = field(default_factory=list)
    error: str = ""


def _pve_fetch(cfg: Proxmox) -> PVEStatus:
    if not cfg.token:
        return PVEStatus(ok=False, error="no PVE token (set PVE_TOKEN)")
    url = cfg.host.rstrip("/") + "/api2/json/cluster/resources?type=vm"
    req = urllib.request.Request(url, headers={
        "Authorization": f"PVEAPIToken={cfg.user}!{cfg.token_name}={cfg.token}",
    })
    ctx = ssl.create_default_context()
    if not cfg.verify_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=6, context=ctx) as resp:
            data = json.loads(resp.read()).get("data", [])
    except Exception as exc:  # noqa: BLE001
        return PVEStatus(ok=False, error=str(exc).split("\n")[0][:80])
    guests = [
        Guest(
            vmid=int(g.get("vmid", 0)),
            name=g.get("name", "?"),
            kind=g.get("type", "?"),
            status=g.get("status", "?"),
            node=g.get("node", "?"),
            cpu=float(g.get("cpu", 0.0)),
            mem=int(g.get("mem", 0)),
            maxmem=int(g.get("maxmem", 0)),
            uptime=int(g.get("uptime", 0)),
        )
        for g in data
    ]
    guests.sort(key=lambda g: g.vmid)
    return PVEStatus(ok=True, guests=guests)


async def proxmox_status(cfg: Proxmox) -> PVEStatus:
    return await asyncio.to_thread(_pve_fetch, cfg)
