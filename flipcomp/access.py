"""Who may use FlipComp, and how they get in.

The server only listens on this computer. When it is shared through a tunnel,
every request that arrives from outside must carry a personal invite token:
partners open their link once (/join/<token>), the token is stored as a
cookie, and from then on the browser is let in. Removing a person, or giving
them a new link, locks the old link out immediately.

Requests made on this computer itself (not through the tunnel) are the owner.

Stored in team.json at the project root, next to pipeline.json. It holds the
invite tokens and the email password, so it is git-ignored.
"""
from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import secrets
import threading
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "team.json")
COOKIE = "fc_session"

ROLES = ("owner", "partner")
USER_FIELDS = ("name", "email", "daily_email")
SETTING_FIELDS = ("public_url", "smtp_host", "smtp_port", "smtp_user", "smtp_password",
                  "mail_from_name", "tunnel_token")
SECRET_SETTINGS = ("smtp_password", "tunnel_token")
DEFAULT_SETTINGS = {"smtp_host": "smtp.gmail.com", "smtp_port": 587,
                    "mail_from_name": "FlipComp"}

_lock = threading.RLock()


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _load() -> dict:
    try:
        with open(PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    data.setdefault("users", {})
    data.setdefault("settings", {})
    return data


def _save(data: dict) -> None:
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, PATH)


def _public(u: dict) -> dict:
    return {k: v for k, v in u.items() if k != "token"}


# --- people -------------------------------------------------------------------
def owner() -> dict:
    """The owner's record, created on first use."""
    with _lock:
        data = _load()
        for u in data["users"].values():
            if u.get("role") == "owner":
                return u
        try:
            from . import prefs
            name = prefs.load_buyer().get("buyer_name") or "Owner"
        except Exception:
            name = "Owner"
        u = {"id": uuid.uuid4().hex[:10], "name": name, "email": "", "role": "owner",
             "daily_email": True, "token": secrets.token_urlsafe(24), "created": _now()}
        data["users"][u["id"]] = u
        _save(data)
        return u


def list_users() -> list[dict]:
    owner()
    users = list(_load()["users"].values())
    users.sort(key=lambda u: (u.get("role") != "owner", (u.get("name") or "").lower()))
    return users


def get(uid: str) -> dict | None:
    return _load()["users"].get(uid)


def add(name: str, email: str = "", daily_email: bool = True) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("Give the person a name.")
    with _lock:
        data = _load()
        u = {"id": uuid.uuid4().hex[:10], "name": name, "email": (email or "").strip(),
             "role": "partner", "daily_email": bool(daily_email),
             "token": secrets.token_urlsafe(24), "created": _now()}
        data["users"][u["id"]] = u
        _save(data)
        return u


def update(uid: str, **fields) -> dict:
    with _lock:
        data = _load()
        u = data["users"].get(uid)
        if not u:
            raise KeyError("No such person.")
        for k in USER_FIELDS:
            if k in fields and fields[k] is not None:
                u[k] = bool(fields[k]) if k == "daily_email" else str(fields[k]).strip()
        _save(data)
        return u


def save_buyer(uid: str, fields: dict) -> dict:
    """A partner's own letter signature (name, phone, email, company)."""
    with _lock:
        data = _load()
        u = data["users"].get(uid)
        if not u:
            raise KeyError("No such person.")
        for k, v in fields.items():
            if k.startswith("buyer_") and v is not None:
                u[k] = str(v).strip()
        _save(data)
        return u


def new_link(uid: str) -> dict:
    """Replace someone's token. Their old link and cookie stop working."""
    with _lock:
        data = _load()
        u = data["users"].get(uid)
        if not u:
            raise KeyError("No such person.")
        u["token"] = secrets.token_urlsafe(24)
        u["link_reset"] = _now()
        _save(data)
        return u


def remove(uid: str) -> bool:
    with _lock:
        data = _load()
        u = data["users"].get(uid)
        if not u or u.get("role") == "owner":
            return False
        del data["users"][uid]
        _save(data)
        return True


def user_for_token(token: str | None) -> dict | None:
    if not token:
        return None
    for u in _load()["users"].values():
        if u.get("token") and hmac.compare_digest(u["token"], token):
            return u
    return None


def touch(uid: str) -> None:
    """Record when someone last used the app (written at most every 10 minutes)."""
    with _lock:
        data = _load()
        u = data["users"].get(uid)
        if not u:
            return
        now = dt.datetime.now()
        last = u.get("last_seen")
        if last and (now - dt.datetime.fromisoformat(last)).total_seconds() < 600:
            return
        u["last_seen"] = now.isoformat(timespec="seconds")
        _save(data)


# --- settings -----------------------------------------------------------------
def settings() -> dict:
    return {**DEFAULT_SETTINGS, **_load()["settings"]}


def settings_public() -> dict:
    """Settings safe to send to the browser: secrets become a yes/no."""
    s = settings()
    out = {k: v for k, v in s.items() if k not in SECRET_SETTINGS}
    for k in SECRET_SETTINGS:
        out[k + "_set"] = bool(s.get(k))
    return out


def save_settings(**fields) -> dict:
    with _lock:
        data = _load()
        s = data["settings"]
        for k in SETTING_FIELDS:
            if k not in fields or fields[k] is None:
                continue
            v = fields[k]
            # A blank secret means "keep what is saved"; there is a separate clear.
            if k in SECRET_SETTINGS and v == "":
                continue
            if k == "smtp_port":
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    continue
            elif k == "public_url":
                v = str(v).strip().rstrip("/")
                if v and not v.startswith(("https://", "http://")):
                    v = "https://" + v
            else:
                v = str(v).strip()
                if k == "smtp_password":
                    v = v.replace(" ", "")  # Google shows app passwords in groups of four
            s[k] = v
        for k in fields.get("clear") or []:
            if k in SETTING_FIELDS:
                s.pop(k, None)
        _save(data)
    return settings_public()


def set_quick_url(url: str | None) -> None:
    """The temporary address a quick tunnel was given this time it started."""
    with _lock:
        data = _load()
        if url:
            data["settings"]["quick_url"] = url
            data["settings"]["quick_url_seen"] = _now()
        else:
            data["settings"].pop("quick_url", None)
        _save(data)


def base_url() -> str | None:
    """Where partners reach the app: a fixed address if one is set, otherwise
    the quick tunnel's current address."""
    s = settings()
    return s.get("public_url") or s.get("quick_url") or None


def join_link(u: dict) -> str | None:
    base = base_url()
    return f"{base}/join/{u['token']}" if base and u.get("token") else None
