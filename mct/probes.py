"""Telemetry sources: Tailscale, SSH probe, Proxmox API.

Everything here is async and never raises — failures come back as a result
object with `ok=False` so the UI can paint it dim instead of crashing.
"""
from __future__ import annotations

import asyncio
import json
import re
import shlex
import ssl
import time
import urllib.request
from dataclasses import dataclass, field

from .inventory import Proxmox, Site, Unit
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


_PONG = re.compile(r"via (\S+) in (\d+(?:\.\d+)?)ms")


async def ts_ping(host: str, count: int = 3, timeout: float = 12.0) -> tuple[int, str] | None:
    """(rtt_ms, path) from `tailscale ping`: path is "direct" or the relay
    name, e.g. "nyc". Pinging also prompts Tailscale to upgrade to a direct
    path, so the number settles on the real transport after a round or two."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tailscale", "ping", "-c", str(count), "--timeout", "3s", host,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except Exception:  # noqa: BLE001
        return None
    last = None
    for line in out.decode(errors="ignore").splitlines():
        m = _PONG.search(line)
        if m:
            via, ms = m.group(1), int(float(m.group(2)))
            path = via[5:-1].lower() if via.startswith("DERP(") else "direct"
            last = (ms, path)
    return last


async def tcp_rtt(host: str, port: int = 22, timeout: float = 3.0) -> int | None:
    """Milliseconds for a bare TCP connect — the network, nothing else."""
    t0 = time.perf_counter()
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    except Exception:  # noqa: BLE001
        return None
    ms = int((time.perf_counter() - t0) * 1000)
    w.close()
    try:
        await w.wait_closed()
    except Exception:  # noqa: BLE001
        pass
    return ms


@dataclass
class Probe:
    ok: bool
    latency_ms: int = 0            # whole probe: connect + ssh auth + script
    rtt_ms: int | None = None      # tailscale ping (falls back to a TCP connect)
    path: str = ""                 # "direct", or the DERP relay name
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


# ----------------------------------------------------------------- http sites


@dataclass
class HttpResult:
    ok: bool
    status: int = 0
    latency_ms: int = 0            # whole request (connect + tls + first byte)
    connect_ms: int | None = None  # TCP connect only — the network
    tls_ms: int | None = None      # TLS handshake
    ttfb_ms: int | None = None     # request sent → first response byte (the app)
    tls_days: int | None = None    # days until the cert expires (https only)
    error: str = ""
    at: float = 0.0


def _http_fetch(site: Site) -> HttpResult:
    """One GET with the phases timed separately: TCP connect (network), TLS
    handshake, and time-to-first-byte (the application). Follows up to 5
    redirects; the timings reported are for the final hop."""
    import datetime
    import http.client
    import socket
    import urllib.parse

    url, hops = site.url, 0
    t_start = time.perf_counter()
    status, body, days = 0, b"", None
    connect_ms = tls_ms = ttfb_ms = None
    while True:
        u = urllib.parse.urlsplit(url)
        host, port = u.hostname or "", u.port or (443 if u.scheme == "https" else 80)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        try:
            t0 = time.perf_counter()
            sock = socket.create_connection((host, port), timeout=site.timeout)
            connect_ms = int((time.perf_counter() - t0) * 1000)
            if u.scheme == "https":
                ctx = ssl.create_default_context()
                t1 = time.perf_counter()
                sock = ctx.wrap_socket(sock, server_hostname=host)
                tls_ms = int((time.perf_counter() - t1) * 1000)
                try:
                    exp = datetime.datetime.strptime(sock.getpeercert()["notAfter"], "%b %d %H:%M:%S %Y %Z")
                    days = (exp - datetime.datetime.utcnow()).days
                except Exception:  # noqa: BLE001
                    days = None
            conn = http.client.HTTPConnection(host, port, timeout=site.timeout)
            conn.sock = sock
            t2 = time.perf_counter()
            conn.request("GET", path, headers={"User-Agent": "fennia-mct/1.0", "Host": u.netloc,
                                                "Accept": "*/*", "Connection": "close"})
            resp = conn.getresponse()
            ttfb_ms = int((time.perf_counter() - t2) * 1000)
            status = resp.status
            if 300 <= status < 400 and resp.getheader("Location") and hops < 5:
                url = urllib.parse.urljoin(url, resp.getheader("Location"))
                hops += 1
                conn.close()
                continue
            body = resp.read(65536) if site.contains else b""
            conn.close()
        except Exception as exc:  # noqa: BLE001
            msg = str(getattr(exc, "reason", exc)).split("\n")[0]
            return HttpResult(ok=False, latency_ms=int((time.perf_counter() - t_start) * 1000),
                              connect_ms=connect_ms, tls_ms=tls_ms, error=msg[:80], at=time.time())
        break
    total = int((time.perf_counter() - t_start) * 1000)
    ok = (status == site.expect) if site.expect else (200 <= status < 400)
    err = "" if ok else f"HTTP {status}"
    if ok and site.contains and site.contains.encode() not in body:
        ok, err = False, f"body lacks {site.contains!r}"
    return HttpResult(ok=ok, status=status, latency_ms=total, connect_ms=connect_ms, tls_ms=tls_ms,
                      ttfb_ms=ttfb_ms, tls_days=days, error=err, at=time.time())


async def http_check(site: Site) -> HttpResult:
    return await asyncio.to_thread(_http_fetch, site)


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
