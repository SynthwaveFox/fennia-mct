"""`mct enroll` — pubkey-enable every unit in the inventory, safely.

Per unit, three phases:
  1. bootstrap  ssh in with whatever works today (password is fine), make sure
                sshd is installed + enabled + running, add the public key to
                authorized_keys with correct ownership/permissions.
  2. verify     BatchMode key-only login with the identity MCT will use.
  3. harden     ONLY if verify passed: drop-in sshd config — key-only auth,
                no passwords, root prohibit-password — validated with `sshd -t`
                before reload. Skipped with --no-harden.

Nothing here can lock you out: password auth is disabled only after a key
login has succeeded on that very host, and the drop-in is syntax-checked
before sshd is reloaded.
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from rich.console import Console

from .inventory import Inventory, Unit, load_inventory
from .keys import KEYS_DIR, resolve_identity, ssh_identity_args
from .theme import AMBER, DIM, ORANGE, PEACH, RED

con = Console(highlight=False)

# ------------------------------------------------------------------ remote script
# POSIX sh, runs as root (directly, or via sudo). $1 = mode, $2 = login user.
REMOTE = r'''#!/bin/sh
set -eu
MODE="$1"; LOGIN_USER="$2"
PUBKEY='__PUBKEY__'
ROOT_LOGIN='__ROOT_LOGIN__'
LISTEN='__LISTEN__'
TS_AUTHKEY='__TS_AUTHKEY__'
TS_TAGS='__TS_TAGS__'
TS_HOSTNAME='__TS_HOSTNAME__'
CT_KIND='__CT_KIND__'

say() { printf '%s\n' "$*"; }

have() { command -v "$1" >/dev/null 2>&1; }

svc_name() {
  if have systemctl; then
    if systemctl list-unit-files 2>/dev/null | grep -q '^sshd\.service'; then echo sshd; else echo ssh; fi
  else
    echo sshd
  fi
}

is_truenas() { have midclt && [ -x /usr/bin/midclt ]; }

truenas_py() {
  # $1 = python snippet; runs with json + midclt helper
  python3 - "$LOGIN_USER" "$PUBKEY" "$ROOT_LOGIN" "$MODE" <<'PYEOF'
import ast, json, subprocess, sys
user, pubkey, root_login, mode = sys.argv[1:5]
def call(method, *args):
    argv = ["midclt", "call", method, *[json.dumps(a) for a in args]]
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"!! midclt {method} failed (rc {r.returncode}): {r.stderr.strip() or r.stdout.strip()}")
    out = r.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        pass
    try:                                   # midclt prints Python literals for scalars (True/None)
        return ast.literal_eval(out)
    except (ValueError, SyntaxError):
        return out
if mode == "bootstrap":
    svc = call("service.query", [["service", "=", "ssh"]])[0]
    if not svc["enable"]:
        call("service.update", "ssh", {"enable": True})
    call("service.start", "ssh")
    print("sshd active (truenas middleware)")
    u = call("user.query", [["username", "=", user]])[0]
    keys = [k for k in (u.get("sshpubkey") or "").splitlines() if k.strip()]
    if pubkey in keys:
        print(f"key already present for {user}")
    else:
        call("user.update", u["id"], {"sshpubkey": "\n".join(keys + [pubkey]) + "\n"})
        print(f"key added for {user}")
elif mode == "harden":
    cur = call("ssh.config")
    upd = {"passwordauth": False, "kerberosauth": False}
    if "rootlogin" in cur:                     # older SCALE
        upd["rootlogin"] = root_login == "yes"
    call("ssh.update", upd)
    call("service.restart", "ssh")
    print("sshd hardened via middleware: passwordauth=off")
PYEOF
}

ensure_sshd() {
  if ! have sshd && [ ! -x /usr/sbin/sshd ]; then
    say "installing openssh server"
    if   have apt-get; then DEBIAN_FRONTEND=noninteractive apt-get update -qq && apt-get install -y -qq openssh-server
    elif have pacman;  then pacman -Sy --noconfirm --needed openssh
    elif have apk;     then apk add --no-cache openssh
    elif have dnf;     then dnf install -y -q openssh-server
    elif have zypper;  then zypper -n install openssh
    else say "!! no known package manager; install sshd manually"; exit 2
    fi
  fi
  if have systemctl; then
    s=$(svc_name)
    systemctl enable --now "$s" >/dev/null 2>&1 || systemctl start "$s"
    systemctl is-active --quiet "$s" && say "sshd active ($s)"
  elif have rc-service; then
    rc-update add sshd default >/dev/null 2>&1 || true
    rc-service sshd start >/dev/null 2>&1 || true
    rc-service sshd status | grep -q started && say "sshd active (openrc)"
  else
    say "?? unknown init; make sure sshd is running"
  fi
}

install_key() {
  home=$(getent passwd "$LOGIN_USER" | cut -d: -f6)
  [ -n "$home" ] || { say "!! no home dir for $LOGIN_USER"; exit 2; }
  grp=$(id -gn "$LOGIN_USER")
  install -d -m 700 -o "$LOGIN_USER" -g "$grp" "$home/.ssh"
  ak="$home/.ssh/authorized_keys"
  touch "$ak"
  if grep -qxF "$PUBKEY" "$ak"; then
    say "key already present for $LOGIN_USER"
  else
    printf '%s\n' "$PUBKEY" >> "$ak"
    say "key added for $LOGIN_USER"
  fi
  # Proxmox links this into /etc/pve/priv (pmxcfs): already 0600 root, and
  # chown/chmod there are refused — skip perms on symlinks, tolerate elsewhere
  if [ ! -L "$ak" ]; then
    chmod 600 "$ak" 2>/dev/null || say "?? could not chmod $ak"
    chown "$LOGIN_USER:$grp" "$ak" 2>/dev/null || say "?? could not chown $ak"
  fi
}

harden() {
  cfg=/etc/ssh/sshd_config
  listen_line=""
  if [ "$LISTEN" = "tailscale" ] && have tailscale; then
    ip=$(tailscale ip -4 2>/dev/null | head -1)
    [ -n "$ip" ] && listen_line="ListenAddress $ip"
  fi
  block="# managed by mct enroll
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PermitRootLogin $ROOT_LOGIN
PermitEmptyPasswords no
X11Forwarding no
UseDNS no
GSSAPIAuthentication no
$listen_line"
  if grep -Eq '^\s*Include\s+/etc/ssh/sshd_config\.d/' "$cfg" 2>/dev/null; then
    mkdir -p /etc/ssh/sshd_config.d
    printf '%s\n' "$block" > /etc/ssh/sshd_config.d/10-mct.conf
    say "wrote /etc/ssh/sshd_config.d/10-mct.conf"
  else
    # no Include support (alpine, old debian): prepend a marked block so it wins
    tmp=$(mktemp)
    { printf '%s\n# end mct\n' "$block"; sed '/^# managed by mct enroll$/,/^# end mct$/d' "$cfg"; } > "$tmp"
    cat "$tmp" > "$cfg"; rm -f "$tmp"
    say "updated $cfg (inline block)"
  fi
  if ! sshd -t 2>/tmp/mct-sshd-t; then
    say "!! sshd -t failed, rolling back:"; cat /tmp/mct-sshd-t
    rm -f /etc/ssh/sshd_config.d/10-mct.conf
    sed -i '/^# managed by mct enroll$/,/^# end mct$/d' "$cfg"
    exit 3
  fi
  if have systemctl; then systemctl reload "$(svc_name)" 2>/dev/null || systemctl restart "$(svc_name)"
  elif have rc-service; then rc-service sshd reload 2>/dev/null || rc-service sshd restart
  fi
  say "sshd reloaded: key-only, root=$ROOT_LOGIN${listen_line:+, $listen_line}"
}

tailscale_join() {
  if [ ! -e /dev/net/tun ]; then
    if [ "$CT_KIND" = "incus" ]; then
      say "!! no /dev/net/tun — on the TrueNAS host run:"
      say "     incus config device add $TS_HOSTNAME tun unix-char source=/dev/net/tun path=/dev/net/tun"
      say "   (no restart needed) then re-run."
    else
      say "!! no /dev/net/tun — this looks like an LXC. On the Proxmox host add to /etc/pve/lxc/<id>.conf:"
      say "     lxc.cgroup2.devices.allow: c 10:200 rwm"
      say "     lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file"
      say "   then restart the container and re-run."
    fi
    exit 4
  fi
  if ! have tailscale; then
    say "installing tailscale"
    if   have pacman; then pacman -Sy --noconfirm --needed tailscale
    elif have apk;    then apk add --no-cache tailscale
    elif have curl;   then curl -fsSL https://tailscale.com/install.sh | sh
    elif have wget;   then wget -qO- https://tailscale.com/install.sh | sh
    else say "!! need curl or wget to fetch tailscale"; exit 2
    fi
  fi
  if have systemctl; then systemctl enable --now tailscaled >/dev/null 2>&1
  elif have rc-service; then rc-update add tailscale default >/dev/null 2>&1; rc-service tailscale start >/dev/null 2>&1
  fi
  state=$(tailscale status --json 2>/dev/null | sed -n 's/.*"BackendState": *"\([A-Za-z]*\)".*/\1/p' | head -1)
  cur=$(tailscale status --json 2>/dev/null | sed -n 's/.*"HostName": *"\([^"]*\)".*/\1/p' | head -1)
  if [ "$state" = "Running" ] && [ "$cur" = "$TS_HOSTNAME" ]; then
    say "tailscale already up as $TS_HOSTNAME ($(tailscale ip -4 | head -1))"
    return 0
  fi
  if [ -n "$TS_TAGS" ]; then
    tailscale up --auth-key="$TS_AUTHKEY" --hostname="$TS_HOSTNAME" --accept-dns=true --advertise-tags="$TS_TAGS" --reset
  else
    tailscale up --auth-key="$TS_AUTHKEY" --hostname="$TS_HOSTNAME" --accept-dns=true --reset
  fi
  say "tailscale up: $TS_HOSTNAME = $(tailscale ip -4 | head -1)${TS_TAGS:+  [$TS_TAGS]}"
}

if is_truenas; then
  if [ "$MODE" = "tailscale" ]; then
    say "!! TrueNAS: install Tailscale as an app from the Apps catalog (host network), not via this script"
    exit 5
  fi
  truenas_py
  exit 0
fi

case "$MODE" in
  bootstrap) ensure_sshd; install_key ;;
  tailscale) tailscale_join ;;
  harden)    harden ;;
  *) say "bad mode"; exit 2 ;;
esac
'''

RUNNER = (
    "u=$(id -un); "
    "if [ \"$(id -u)\" = 0 ]; then sh /tmp/mct-enroll.sh {mode} \"$u\"; "
    "else sudo -p '[sudo on %h] password: ' sh /tmp/mct-enroll.sh {mode} \"$u\"; fi; "
    "rc=$?; rm -f /tmp/mct-enroll.sh; exit $rc"
)


# ------------------------------------------------------------------ helpers

def ok(msg: str) -> None:
    con.print(f"[{ORANGE}]\\[ OK ][/] {msg}")


def warn(msg: str) -> None:
    con.print(f"[{AMBER}]\\[ .. ][/] {msg}")


def fail(msg: str) -> None:
    con.print(f"[{RED}]\\[ !! ][/] {msg}")


def public_key_for(identity: str, explicit_pub: str | None) -> str:
    """The public key line to install: --pub, else <identity>.pub, else derive
    it from the private key with ssh-keygen -y."""
    if explicit_pub:
        p = Path(explicit_pub).expanduser()
        return p.read_text(encoding="utf-8").strip() if p.is_file() else explicit_pub.strip()
    priv = resolve_identity(identity) if identity else Path.home() / ".ssh" / "id_ed25519"
    pub = priv.with_name(priv.name + ".pub")
    if pub.is_file():
        return pub.read_text(encoding="utf-8").strip()
    if priv.is_file():
        out = subprocess.run(["ssh-keygen", "-y", "-f", str(priv)], capture_output=True, text=True)
        if out.returncode == 0:
            return out.stdout.strip()
    raise SystemExit(
        f"mct enroll: no public key found for {priv}.\n"
        f"  generate one:   mct keys gen mct_ed25519     (then set  identity: mct_ed25519)\n"
        f"  or pass one:    --pub ~/.ssh/id_ed25519.pub"
    )


def run_remote(target: str, mode: str, script: str, identity: str, batch: bool,
               via: tuple[str, str] | None = None) -> tuple[int, str]:
    """Copy the script to /tmp on the host, then run it (root or sudo).

    With `via` = (host, exec prefix) the script is staged on the host, copied
    into the container with `<prefix> sh -c 'cat > …'`, and run there as root.
    sudo (if needed) only happens in the -t step, so it can prompt."""
    base = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=8"]
    if batch:
        base += ["-o", "BatchMode=yes", *ssh_identity_args(identity)]
    elif identity:
        # interactive bootstrap: still offer the configured key first, then
        # let ssh fall through to a password prompt if the key isn't accepted
        base += ["-i", str(resolve_identity(identity))]
    host = via[0] if via else target
    stage = "/tmp/mct-enroll.stage.sh" if via else "/tmp/mct-enroll.sh"
    # bytes, not text=True: on Windows text mode would rewrite LF as CRLF and
    # the remote sh chokes on "set -eu<CR>"
    up = subprocess.run([*base, host, f"cat > {stage}"],
                        input=script.replace("\r\n", "\n").encode("utf-8"), capture_output=True)
    if up.returncode != 0:
        return up.returncode, up.stderr.decode(errors="ignore").strip()
    if via:
        prefix = via[1]
        cmd = (
            'mctS() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo -H "$@"; fi; }; '
            f"mctS {prefix} sh -c 'cat > /tmp/mct-enroll.sh' < {stage} && "
            f"mctS {prefix} sh -c 'sh /tmp/mct-enroll.sh {mode} root; rc=$?; rm -f /tmp/mct-enroll.sh; exit $rc'; "
            f"rc=$?; rm -f {stage}; exit $rc"
        )
    else:
        cmd = RUNNER.format(mode=mode)
    proc = subprocess.run([*base, "-t", host, cmd], text=True)
    return proc.returncode, ""


def verify(target: str, identity: str, attempts: int = 2) -> tuple[bool, str]:
    """Key-only login. Bounded: ssh can stall past ConnectTimeout while sshd
    is reloading, so cap the whole thing and retry once."""
    why = ""
    for i in range(attempts):
        try:
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                 "-o", "StrictHostKeyChecking=accept-new",
                 "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
                 *ssh_identity_args(identity), target, "echo mct-ok"],
                capture_output=True, text=True, timeout=20,
            )
        except subprocess.TimeoutExpired:
            why = "timed out"
            time.sleep(2)
            continue
        if "mct-ok" in proc.stdout:
            return True, ""
        why = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else f"exit {proc.returncode}"
        if i + 1 < attempts:
            time.sleep(2)
    return False, why


# ------------------------------------------------------------------ main

def enroll_unit(inv: Inventory, u: Unit, pub: str, args: argparse.Namespace) -> bool:
    """bootstrap → (tailscale) → verify → harden.  Tailscale comes before
    verify on purpose: a container reached via its host has no route of its
    own until it is on the tailnet."""
    identity = inv.identity_for(u)
    target = u.lan if args.lan and u.lan else u.ssh
    via: tuple[str, str] | None = None
    run_identity = identity
    if u.via:
        host = inv.by_name(u.via)
        host_target = (host.lan if args.lan and host and host.lan else host.ssh) if host else u.via
        via = (host_target, u.via_exec)
        run_identity = inv.identity_for(host) if host else inv.identity
    con.print(f"\n[{PEACH}]▌{u.name}▐[/] [{DIM}]→ {target}"
              f"{'  via ' + via[0] + ': ' + via[1] if via else ''}"
              f"{'  key=' + identity if identity else '  key=ssh default'}[/]")

    script = (REMOTE.replace("__PUBKEY__", pub)
                    .replace("__ROOT_LOGIN__", args.root_login)
                    .replace("__LISTEN__", "tailscale" if args.tailscale_only else "")
                    .replace("__TS_AUTHKEY__", args.ts_key or "")
                    .replace("__TS_TAGS__", args.ts_tags or "")
                    .replace("__TS_HOSTNAME__", u.tailscale)
                    .replace("__CT_KIND__", "incus" if u.via_exec.startswith("incus") else
                             "pct" if u.via_exec.startswith("pct") else ""))

    def remote(mode: str, batch: bool) -> tuple[int, str]:
        return run_remote(target, mode, script, run_identity, batch=batch, via=via)

    already, why = verify(target, identity)
    if not already and why and "timed out" not in why and "Connection" not in why:
        warn(f"key login not accepted yet ({why}) — bootstrapping")
    if already and args.pub:
        # adding another device's key: we can get in, so install it now
        if args.dry_run:
            warn("would: install the given public key (through our own key)")
        else:
            rc, err = remote("bootstrap", batch=True)
            if rc != 0:
                fail(f"key install failed (exit {rc}) {err}")
                return False
            ok("extra key installed")
    elif already:
        ok("key login already works")
    elif args.dry_run:
        warn("would: ensure sshd running, add key" + (" (through host exec)" if via else " (interactive ssh, password ok)"))
    else:
        # via: host must already accept our key (BatchMode); direct: anything goes
        rc, err = remote("bootstrap", batch=bool(via))
        if rc != 0:
            fail(f"bootstrap failed (exit {rc}) {err}")
            return False
        ok("sshd running, key installed")

    if args.ts_key:
        if args.dry_run:
            warn(f"would: install tailscale, `tailscale up` as {u.tailscale}"
                 f"{' tags=' + args.ts_tags if args.ts_tags else ''}")
        else:
            rc, err = remote("tailscale", batch=not (via is None and not already))
            if rc == 5:
                warn("TrueNAS host: install Tailscale from the Apps catalog instead")
            elif rc == 4:
                fail("no /dev/net/tun — container needs TUN passthrough (instructions printed above)")
                return False
            elif rc != 0:
                fail(f"tailscale join failed (exit {rc}) {err}")
                return False
            else:
                ok(f"tailscale joined as {u.tailscale}")

    if not already and not args.dry_run:
        good, why = verify(target, identity)
        if not good:
            fail(f"key login to {target} fails: {why or 'unknown'} — leaving password auth ON")
            if via and not args.ts_key and "timed out" in why:
                warn("no route to the container — is it on the tailnet? (re-run with --ts-key to join it)")
            return False
        ok("key login verified")

    if args.no_harden:
        warn("harden skipped (--no-harden)")
        return True
    if args.dry_run:
        warn(f"would: harden sshd (key-only, root={args.root_login}"
             f"{', tailscale-only listen' if args.tailscale_only else ''}), sshd -t, reload")
        return True
    rc, err = remote("harden", batch=True)
    if rc != 0:
        fail(f"harden failed (exit {rc}) {err} — config rolled back on host")
        return False
    good, why = verify(target, identity)
    if good:
        ok("hardened, key login re-verified")
        return True
    fail(f"login broke after harden: {why} — check the host console")
    return False


def cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="mct enroll", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("units", nargs="*", help="unit names (default: all)")
    ap.add_argument("-i", "--inventory")
    ap.add_argument("--pub", help="public key line or .pub file (default: from identity)")
    ap.add_argument("--lan", action="store_true", help="connect via each unit's lan: alias")
    ap.add_argument("--no-harden", action="store_true", help="install key + sshd only; keep password auth")
    ap.add_argument("--root-login", default="prohibit-password", choices=["prohibit-password", "no", "yes"])
    ap.add_argument("--tailscale-only", action="store_true",
                    help="bind sshd to the host's tailscale IP (breaks LAN ssh — be sure)")
    ap.add_argument("--ts-key", default=None, metavar="tskey-auth-…",
                    help="join each host to the tailnet with this auth key (or set $TS_AUTHKEY)")
    ap.add_argument("--ts-tags", default="tag:server", help="tags to advertise (default: tag:server)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if args.ts_key is None and os.environ.get("TS_AUTHKEY"):
        args.ts_key = os.environ["TS_AUTHKEY"]
    if args.tailscale_only and not args.ts_key:
        warn("--tailscale-only without --ts-key: hosts must already be on the tailnet")

    inv = load_inventory(args.inventory)
    units = inv.units if not args.units else [u for u in inv.units if u.name in args.units]
    missing = set(args.units) - {u.name for u in units}
    if missing:
        con.print(f"[{RED}]unknown units: {', '.join(sorted(missing))}[/]")
        return 1
    if not units:
        con.print(f"[{RED}]no units in inventory[/]")
        return 1
    units = sorted(units, key=lambda u: bool(u.via))   # hosts before the containers they carry

    pub = public_key_for(inv.identity, args.pub)
    con.print(f"[{PEACH}]public key:[/] [{DIM}]{pub[:40]}…{pub[-24:]}[/]")
    if args.tailscale_only:
        warn("--tailscale-only: sshd will stop answering on LAN addresses")

    results = {u.name: enroll_unit(inv, u, public_key_for(inv.identity_for(u), args.pub) if inv.identity_for(u) != inv.identity else pub, args)
               for u in units}
    con.print()
    good = [n for n, r in results.items() if r]
    bad = [n for n, r in results.items() if not r]
    con.print(f"[{ORANGE}]{len(good)} enrolled[/]" + (f"  [{RED}]{len(bad)} failed: {', '.join(bad)}[/]" if bad else ""))
    return 0 if not bad else 1
