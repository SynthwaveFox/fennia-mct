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
mct/probes.py     tailscale_status / ssh_probe / proxmox_status (all async, never raise)
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
| greetd | `contrib/session-chooser.patch` | "master control" as a boot target next to niri |

Windows taskbar/Start shortcut: target `wt.exe -p "FENNIA MCT"`.
