# FENNIA MCT — Master Control Terminal

SSH selector + live fleet status for the FX-2 squadron. Boots like your
`session-chooser`, lists every unit with Tailscale link state, SSH-probes
uptime / load / mem / disk / systemd services, and drops you into `ssh` on
Enter. Orange rice, double borders, Mocha base.

```
▌FENNIA▐ MASTER CONTROL  v0.1.0        TAILNET 4/5 online  as laptop  FX-2 SQUADRON  ▐▛▜▌▐▛▜▌:▐▛▜▌
╔═ UNITS ═════════════════════╗╔═ TELEMETRY ═══════════════════════════════════════════╗
║   UNIT     TAG    LINK  RTT ║║ ● PVE01   pve   #core                                 ║
║ ● pve01    core   100.. 12ms║║ LINK  pve01.tail.ts.net  100.64.0.1  linux  online     ║
║ ● pihole   net    100..  8ms║║ UP    41d 3h   LOAD 0.42 0.38 0.35   RTT 12ms          ║
║ ◐ media    lab    100..  ✕  ║║ MEM   ▰▰▰▰▰▰▱▱▱▱  61%   19.4G / 31.3G                  ║
║ ○ nas      stor.  offline   ║║ DISK  ▰▰▰▱▱▱▱▱▱▱  34%   31G / 93G on /                 ║
╚═════════════════════════════╝║ SERVICES  ● pveproxy  ● pvedaemon  ○ zfs-zed            ║
╔═ LOG ═══════════════════════════════════════════════════════════════════════════════════╗
║ 14:32:01  tailscale  media went offline                                                 ║
```

## Install

```bash
git clone <this repo> ~/src/fennia-mct && cd ~/src/fennia-mct
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
cp inventory.example.yaml ~/.config/mct/inventory.yaml   # then edit
mct
```

`mct --no-boot` skips the boot sequence. `-i path.yaml` picks an inventory.
Inventory search order: `$MCT_INVENTORY`, `./inventory.yaml`,
`~/.config/mct/inventory.yaml`, `~/.mct.yaml`, then `~/.ssh/config` Host entries.

## Site checks

Add `sites:` to the inventory and a **SITES** panel appears under TELEMETRY:
HTTP status, response time, and days until the TLS cert expires, checked from
the machine running MCT every `http_interval` seconds (default 60).

```yaml
http_interval: 60
sites:
  - name: blog
    url: https://blog.example.com
    unit: media                 # optional: also listed in that unit's telemetry
  - name: jellyfin
    url: https://jellyfin.example.com/health
    expect: 200                 # exact status; default is any 2xx/3xx
    contains: Healthy           # body must contain this
```

Up/down transitions and certs under 14 days go to the log. `r` re-checks now.

## `mct ssh`

Plain ssh, but with the unit's alias, user and key resolved from the inventory:

```bash
mct ssh pve                              # interactive
mct ssh truenas-scale sudo incus list    # one command
mct ssh --lan nas                        # use the lan: alias
mct ssh --via sandbox                    # shell through the host's exec
```

## Keys

MCT just runs `ssh <alias>`, so anything OpenSSH would use works: `~/.ssh/id_*`,
`IdentityFile` in `~/.ssh/config`, or an agent. The probe runs with
`BatchMode=yes` and never prompts — passphrase keys must be in an agent.

If you keep keys in a password manager and copy them onto each machine by hand,
`mct keys add` does the paste correctly (permissions, line endings, trailing
newline) into `~/.config/mct/keys/` — outside every repo on purpose:

```bash
mct keys add mct_ed25519        # paste, then Ctrl-D  (Ctrl-Z Enter on Windows)
mct keys add nas --from ~/Downloads/nas_key --pub "ssh-ed25519 AAAA… nas"
mct keys list
```

Then point the inventory at a key — globally or per unit (the edit form has an
`identity` field too):

```yaml
identity: mct_ed25519          # default for all units
units:
  - name: nas
    identity: nas               # override; a path like ~/.ssh/foo also works
```

MCT passes `-i <key> -o IdentitiesOnly=yes` to both the probe and the
interactive session. Leave `identity` unset and it behaves like plain ssh.

## Enrolling servers (`mct enroll`)

Pubkey-enables every unit in the inventory without ever locking you out:

```bash
mct keys gen                      # ~/.config/mct/keys/mct_ed25519  (once per client device)
# set  identity: mct_ed25519  in the inventory
mct enroll --dry-run              # what would happen
mct enroll                        # all units;  mct enroll pve01 nas  for a subset
```

Per unit: **bootstrap** (ssh in with whatever works today — password is fine —
install sshd if missing, enable + start it, add the key to `authorized_keys`),
**verify** (BatchMode key-only login), then **harden** only if verify passed:
a drop-in `/etc/ssh/sshd_config.d/10-mct.conf` with key-only auth, no
passwords, `PermitRootLogin prohibit-password`, syntax-checked with `sshd -t`
and rolled back if that fails, then reload. Works as root or via `sudo`
(prompts once per host). Debian/Ubuntu/Proxmox, Arch, Alpine, Fedora.

Flags: `--no-harden` (key + sshd only), `--root-login no`, `--lan` (use each
unit's `lan:` alias), `--tailscale-only` (bind sshd to the tailnet IP — LAN ssh
stops working, so keep the Proxmox console handy), `--pub` (use another key).

### Joining hosts to the tailnet

Add `--ts-key` (or `export TS_AUTHKEY=…`) and enroll also installs Tailscale on
each host and brings it up as the unit's `tailscale:` hostname with the tags you
choose, so MCT's peer matching just works:

```bash
export TS_AUTHKEY=tskey-auth-…            # admin console → Settings → Keys: reusable, tagged tag:server
mct enroll --lan --ts-key "$TS_AUTHKEY"                       # everything, tag:server
mct enroll --lan --ts-tags tag:server,tag:pve pve01           # PVE hosts get both tags
```

Order matters on first run: use `--lan` so the bootstrap goes over the LAN
alias, then once the host is on the tailnet the MagicDNS alias takes over.
LXCs need TUN passed through first — enroll detects the missing `/dev/net/tun`
and prints the two lines for `/etc/pve/lxc/<id>.conf`.

The matching tailnet policy is in `contrib/tailscale-policy.hujson`.

### Containers with no sshd (TrueNAS Instances, Proxmox LXCs)

A fresh Incus instance or LXC has no sshd and no password — the only way in
is `exec` from its host. Give the unit a `via:` and MCT reaches it through
the host until it has its own sshd + Tailscale, and as a fallback after:

```yaml
units:
  - name: truenas
    kind: appliance
    lan: admin@192.168.1.20
  - name: media                     # Incus instance on truenas
    kind: lxc
    via: truenas                    # via_exec defaults to "incus exec media --"
  - name: ct101                     # Proxmox LXC
    kind: lxc
    via: pve01
    via_exec: pct exec 101 --
```

`mct enroll` orders hosts before their containers, runs bootstrap + tailscale
inside the container through the host, then verifies direct key login over
the tailnet and hardens. The host login needs root or passwordless sudo for
the exec command. In the TUI, `Enter` on a via-only unit opens a shell
through the host (`x` forces that even when direct ssh exists).

Missing `/dev/net/tun` in an Incus instance is detected and the fix printed:
`incus config device add <name> tun unix-char source=/dev/net/tun path=/dev/net/tun`
on the TrueNAS host.

### TrueNAS host

TrueNAS is an appliance: its `sshd_config` is generated by the middleware, so
enroll detects it and goes through `midclt` instead — enables the SSH service,
adds the key to the user's *SSH Public Key* field, and turns *Password
Authentication* off. Tailscale on TrueNAS itself is installed from the Apps
catalog, not by enroll (it says so and moves on).

## Adding another device (laptop, WSL, a second desktop)

Each device gets its own key; the fleet already trusts one device, so that one
pushes the newcomer's key out. Four steps.

**On the new device**

```bash
pipx install git+https://github.com/SynthwaveFox/fennia-mct     # 1. install
```

```bash
mkdir -p ~/.config/mct && cp <inventory.yaml from an enrolled device> ~/.config/mct/   # 2. inventory (no secrets in it)
```

```bash
mct keys gen -C mct@laptop      # 3. its own key — distinct comment so authorized_keys stays readable
```

Copy the `ssh-ed25519 …` line it prints.

**On a device that already has access**

```bash
mct enroll --pub "ssh-ed25519 AAAA… mct@laptop"     # 4. installs it on every unit, no passwords
```

Back on the new device: `mct`.

**WSL:** WSL2 shares Windows' network so it is already on the tailnet, but has
no `tailscale` binary and MCT calls one for the online/offline column. Point it
at the Windows one — WSL can exec Windows binaries directly:

```bash
sudo ln -s "/mnt/c/Program Files/Tailscale/tailscale.exe" /usr/local/bin/tailscale
```

The inventory can be copied straight across: `cp /mnt/c/Users/<you>/.config/mct/inventory.yaml ~/.config/mct/`.

**Losing a device:** remove its line (by comment) from `authorized_keys` on each
unit — `mct ssh <unit> "sed -i '/mct@laptop$/d' ~/.ssh/authorized_keys"` — and
on TrueNAS from the user's *SSH Public Key* field in the UI.

## The stack it assumes

1. **Tailscale on every unit.** `tailscale status --json` is the source of
   truth for online/offline — no agents, no polling cost. LXCs need TUN:
   ```
   # /etc/pve/lxc/<id>.conf
   lxc.cgroup2.devices.allow: c 10:200 rwm
   lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
   ```
   Tag units (`tag:pve`, `tag:lab`) and write ACLs so only your admin devices
   reach port 22. Enable Tailnet Lock.

2. **OpenSSH, keys only, bound to the tailnet.** On each unit:
   ```
   PasswordAuthentication no
   KbdInteractiveAuthentication no
   PermitRootLogin prohibit-password     # Proxmox host; `no` elsewhere
   ListenAddress 100.x.y.z               # its tailscale IP
   AllowUsers <you>
   ```
   One `ed25519` key per client device. The probe uses `BatchMode=yes`, so
   the key has to be in `ssh-agent` (or passphrase-less) — it will never
   prompt, it just reports unreachable.

3. **`~/.ssh/config` is the address book.** MCT only knows aliases; users,
   ports, keys and jump hosts live here:
   ```
   Host pve01
       HostName pve01.tail1234.ts.net
       User root
   Host pve01-lan
       HostName 10.0.0.5
       User root
   Host ct-*
       User admin
       ProxyJump pve01
   ```

4. **Proxmox (optional).** Datacenter → Permissions → API Tokens → add
   `root@pam!mct` with privilege separation, grant `PVEAuditor` on `/`, then
   `export PVE_TOKEN=<secret>` and uncomment the `proxmox:` block. The guest
   panel shows every VM/CT even ones without Tailscale.

## Fitting it into the rice

- **greetd session-chooser** — add a line so MCT is a boot target next to niri:
  ```bash
  choice=$(gum choose --header "Select a session:" "niri" "master control" "terminal" "reboot" "power off")
  case "$choice" in
    "master control") exec ~/src/fennia-mct/.venv/bin/mct ;;
  ```
- **ghostty** — `theme = catppuccin-mocha` with `palette = 3=#ff8a00` and
  `palette = 11=#ffb347` keeps the vconsole trick (slot 3 → orange) consistent.
- **Windows Terminal** — `"experimental.retroTerminalEffect": true` in the
  profile gives you scanlines + glow; pair with a JetBrainsMono Nerd Font.
- **Wallpaper-matched name** — `callsign:` and `squadron:` in the inventory
  feed the boot screen ("Welcome back, Commander").

## Layout of the code

```
mct/app.py        main screen, pollers, ssh launch (App.suspend)
mct/boot.py       boot sequence screen — powers on the wordmark, runs the first tailscale poll
mct/wordart.py    baked figlet wordmark (ansi_shadow) + subline
mct/forms.py      add / edit / remove unit modal
mct/probes.py     tailscale_status / ssh_probe / http_check / proxmox_status (all async, never raise)
mct/inventory.py  inventory.yaml loader, ~/.ssh/config fallback
mct/keys.py       ~/.config/mct/keys + `mct keys gen|add|list|path`
mct/enroll.py     `mct enroll` — bootstrap / verify / harden sshd on every unit
mct/theme.py      Textual Theme built from the waybar/mako palette
mct/theme.tcss    layout + double borders
```

## Launching it properly

Install it as a command first, so nothing depends on activating a venv:

```bash
pipx install git+https://github.com/<you>/fennia-mct     # or: uv tool install git+…
mct --version 2>/dev/null || which mct                   # → ~/.local/bin/mct
```

Then wire it into the environment — every snippet is in `contrib/`:

| where | file | what it does |
|---|---|---|
| Windows Terminal | `contrib/windows-terminal.json` | `FENNIA MCT` profile + `Fennia` colour scheme + `Ctrl+Shift+M`; retro CRT effect on |
| niri | `contrib/niri-snippet.kdl` | `Mod+M` spawns it in ghostty with app-id `fennia.mct`, window rule with orange ring |
| fuzzel / rofi | `contrib/fennia-mct.desktop` | launcher entry |
| ghostty | `contrib/ghostty-mct` | palette that matches (slot 3 → `#ff8a00`, like your vconsole trick) |
| greetd | `contrib/session-chooser.patch` | "master control" as a boot target next to niri — runs MCT in ghostty under `cage`, so it's the real UI, not the VT font |

Windows taskbar/Start shortcut: target `wt.exe -p "FENNIA MCT"`.
