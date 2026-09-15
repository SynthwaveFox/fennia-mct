"""Key directory: ~/.config/mct/keys — outside any repo, next to the inventory.

    mct keys add mct_ed25519      # paste the private key, Ctrl-D (Ctrl-Z Enter on Windows)
    mct keys list
    mct keys path

`add` normalises line endings, guarantees the trailing newline OpenSSH wants,
and sets 0600 (owner-only ACL on Windows) so ssh doesn't refuse the file.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

KEYS_DIR = Path.home() / ".config" / "mct" / "keys"


def resolve_identity(identity: str) -> Path:
    """`identity:` in the inventory may be a bare name (looked up in KEYS_DIR)
    or a path (~ expanded)."""
    p = Path(identity).expanduser()
    if p.parent == Path(".") and not p.is_absolute():
        return KEYS_DIR / identity
    return p


def ssh_identity_args(identity: str | None) -> list[str]:
    if not identity:
        return []
    return ["-i", str(resolve_identity(identity)), "-o", "IdentitiesOnly=yes"]


def lock_down(path: Path) -> None:
    if sys.platform == "win32":
        user = os.environ.get("USERNAME", "")
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                       check=False, capture_output=True)
    else:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def write_key(name: str, private: str, public: str = "") -> Path:
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(KEYS_DIR, stat.S_IRWXU)
    body = private.replace("\r\n", "\n").replace("\r", "\n").strip("\n") + "\n"
    priv = KEYS_DIR / name
    priv.write_text(body, encoding="utf-8", newline="\n")
    lock_down(priv)
    if public.strip():
        (KEYS_DIR / f"{name}.pub").write_text(public.strip() + "\n", encoding="utf-8", newline="\n")
    return priv


def list_keys() -> list[Path]:
    if not KEYS_DIR.is_dir():
        return []
    return sorted(p for p in KEYS_DIR.iterdir() if p.is_file() and p.suffix != ".pub")


def cli(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="mct keys", description="MCT key directory (~/.config/mct/keys)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="store a private key pasted on stdin")
    add.add_argument("name", help="file name, e.g. mct_ed25519 — what `identity:` refers to")
    add.add_argument("--pub", help="matching public key line (optional)")
    add.add_argument("--from", dest="src", help="read the key from this file instead of stdin")
    gen = sub.add_parser("gen", help="generate a new ed25519 key in the key dir")
    gen.add_argument("name", nargs="?", default="mct_ed25519")
    gen.add_argument("--comment", "-C", default=None, help="key comment (default: mct@<hostname>)")
    sub.add_parser("list", help="show keys in the key dir")
    sub.add_parser("path", help="print the key dir")
    args = ap.parse_args(argv)

    if args.cmd == "gen":
        import socket
        KEYS_DIR.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(KEYS_DIR, stat.S_IRWXU)
        path = KEYS_DIR / args.name
        if path.exists():
            print(f"mct keys: {path} already exists", file=sys.stderr)
            return 1
        comment = args.comment or f"mct@{socket.gethostname().lower()}"
        rc = subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", str(path), "-C", comment]).returncode
        if rc != 0 or not path.exists():
            return rc or 1
        lock_down(path)
        print(f"  → {path}")
        print(f"public: {path.with_name(path.name + '.pub').read_text(encoding='utf-8').strip()}")
        print(f"set  identity: {args.name}  in the inventory, then  mct enroll")
        return 0

    if args.cmd == "path":
        print(KEYS_DIR)
        return 0
    if args.cmd == "list":
        keys = list_keys()
        if not keys:
            print(f"no keys in {KEYS_DIR}  (run: mct keys add <name>)")
            return 0
        for p in keys:
            print(f"{p.name:<28} {'+pub' if p.with_name(p.name + '.pub').exists() else ''}")
        return 0
    if args.cmd == "add":
        if any(c in args.name for c in "/\\") or args.name.startswith("."):
            print("mct keys: name must be a plain file name", file=sys.stderr)
            return 1
        if args.src:
            private = Path(args.src).expanduser().read_text(encoding="utf-8")
        else:
            if sys.stdin.isatty():
                end = "Ctrl-Z then Enter" if sys.platform == "win32" else "Ctrl-D"
                print(f"paste the private key, then {end}:", file=sys.stderr)
            private = sys.stdin.read()
        if "PRIVATE KEY" not in private:
            print("mct keys: that doesn't look like a private key (no PRIVATE KEY header)", file=sys.stderr)
            return 1
        path = write_key(args.name, private, args.pub or "")
        print(f"  → {path}")
        print(f"reference it with  identity: {args.name}  in the inventory (or per unit)")
        return 0
    return 1
