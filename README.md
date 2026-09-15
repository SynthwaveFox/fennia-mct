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

## Keys

| key     | action                                             |
|---------|----------------------------------------------------|
| `enter` | ssh into the highlighted unit (app suspends, resumes on exit) |
| `l`     | ssh via the unit's `lan:` alias (fallback when the tailnet is down) |
| `r`     | refresh tailscale + probes + proxmox now           |
| `/`     | filter units by name / tag / kind (`esc` clears)   |
| `t`     | `tailscale ping` the unit, result goes to the log  |
| `a`     | add a unit (or click **+ ADD** under the table)    |
| `e`     | edit the highlighted unit (or click **EDIT**) — the form also has REMOVE |
| `q`     | quit                                               |

Add/edit writes straight back to the inventory file it loaded (or to
`~/.config/mct/inventory.yaml` if it came from `~/.ssh/config`). The file is
rewritten by the YAML dumper, so hand-written comments in it don't survive —
keep notes in each unit's `note:` field instead.

## Status glyphs

- `●` orange — tailscale online, probe OK, all services active
- `◐` amber — reachable but a service isn't `active`, or probe failed while tailscale says online
- `○` dim — offline
- `◌` — not in the tailnet and never probed

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
mct/boot.py       boot sequence screen — runs the first tailscale poll
mct/forms.py      add / edit / remove unit modal
mct/probes.py     tailscale_status / ssh_probe / proxmox_status (all async, never raise)
mct/inventory.py  inventory.yaml loader, ~/.ssh/config fallback
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
| greetd | `contrib/session-chooser.patch` | "master control" as a boot target next to niri |

Windows taskbar/Start shortcut: target `wt.exe -p "FENNIA MCT"`.
