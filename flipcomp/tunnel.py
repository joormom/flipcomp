"""Put the local server on the internet through a Cloudflare Tunnel.

The server keeps listening only on this computer; cloudflared makes an
outbound connection to Cloudflare and relays requests back to it, so no router
ports are opened. Two modes:

* Quick tunnel (no account): Cloudflare hands out a random
  https://<words>.trycloudflare.com address that changes every time the tunnel
  starts. Fine to begin with; the daily email carries each person's current link.
* Named tunnel (free Cloudflare account and a domain): a fixed address such as
  https://deals.example.com. Create the tunnel in the Cloudflare dashboard,
  paste its token into the Team tab, and set the public URL.

Access control is not delegated to the tunnel: the app itself refuses anyone
without an invite (see access.py), whichever mode is used.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading

from . import access

QUICK_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

_proc: subprocess.Popen | None = None
_state = {"running": False, "mode": None, "url": None, "error": None, "log": []}


def find_cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\cloudflared\cloudflared.exe"),
        os.path.expandvars(r"%ProgramFiles%\cloudflared\cloudflared.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\cloudflared.exe"),
    ]
    return next((c for c in candidates if os.path.isfile(c)), None)


def status() -> dict:
    return {**{k: v for k, v in _state.items() if k != "log"},
            "installed": bool(find_cloudflared()), "log": _state["log"][-8:]}


def _reader(stream) -> None:
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", "replace").rstrip()
        if not line:
            continue
        _state["log"] = (_state["log"] + [line])[-40:]
        m = QUICK_RE.search(line)
        if m and _state["mode"] == "quick" and _state["url"] != m.group(0):
            _state["url"] = m.group(0)
            access.set_quick_url(_state["url"])
            print(f"\n  Shared at {_state['url']}")
            _print_owner_link()
        if "Registered tunnel connection" in line and _state["mode"] == "named":
            _state["url"] = access.settings().get("public_url")
    _state["running"] = False
    _state["error"] = _state["error"] or "cloudflared stopped"


def _print_owner_link() -> None:
    link = access.join_link(access.owner())
    if link:
        print(f"  Your own link (for your phone): {link}")
        print("  Add partners and copy their links in the Team tab.\n")


def start(port: int) -> dict:
    """Start cloudflared in the background. Returns status()."""
    global _proc
    exe = find_cloudflared()
    if not exe:
        _state["error"] = ("cloudflared is not installed. Install it with: "
                           "winget install --id Cloudflare.cloudflared")
        return status()
    s = access.settings()
    env = dict(os.environ)
    if s.get("tunnel_token"):
        _state["mode"] = "named"
        env["TUNNEL_TOKEN"] = s["tunnel_token"]  # kept off the command line
        cmd = [exe, "tunnel", "--no-autoupdate", "run"]
    else:
        _state["mode"] = "quick"
        access.set_quick_url(None)  # the old random address is gone
        cmd = [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    _proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, env=env, creationflags=flags)
    _state.update(running=True, error=None, url=s.get("public_url") if s.get("tunnel_token") else None)
    threading.Thread(target=_reader, args=(_proc.stdout,), daemon=True).start()
    if _state["mode"] == "named" and _state["url"]:
        print(f"\n  Shared at {_state['url']} (named tunnel)")
        _print_owner_link()
    return status()


def stop() -> None:
    global _proc
    if _proc and _proc.poll() is None:
        _proc.terminate()
        try:
            _proc.wait(timeout=5)
        except Exception:
            _proc.kill()
    _proc = None
    _state.update(running=False)
    if _state["mode"] == "quick":
        access.set_quick_url(None)
