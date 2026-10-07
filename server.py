"""FlipComp web server.

Deliberately stdlib-only. Run `python server.py` for local use, or
`python server.py --share` to also open a Cloudflare Tunnel so partners can
reach it from their own phones and computers.

The server only ever listens on 127.0.0.1. Anything arriving through the
tunnel must carry a personal invite (see flipcomp/access.py); people without
one get a "private tool" page and nothing else.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from flipcomp import (access, dossier, history, leads, letters, mailer, offer, pipeline, prefs, regions,
                      rehab, report, roll_import, search, taxroll, tunnel)
from flipcomp.analyze import analyze
from flipcomp.compengine import CompError
from flipcomp.data import DataError

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
PORT = int(os.environ.get("FLIPCOMP_PORT", "8777"))

NUMERIC_OVERRIDES = {
    "subject_sqft", "subject_beds", "subject_full_baths", "subject_year_built",
    "subject_lot_sqft", "rehab_contingency_pct", "rehab_override_total",
    "rent_override", "management_pct", "refi_rate_pct", "refi_ltv_pct",
} | set(offer.DEFAULTS)

# Routes only the owner may use: who has access, email settings, the skip
# list (it purges history for everyone), bulk imports of owner data, and
# deleting deals (partners mark them Dead instead).
OWNER_GET = {"/api/team"}
OWNER_POST = {"/api/team/add", "/api/team/update", "/api/team/link", "/api/team/remove",
              "/api/team/settings", "/api/team/invite", "/api/team/send-daily", "/api/team/tunnel",
              "/api/prefs", "/api/roll/import", "/api/pipeline/delete"}
POST_ROUTES = {"/api/analyze", "/api/search", "/api/leads", "/api/leads/oscn", "/api/taxcheck",
               "/api/dossier", "/api/prefs", "/api/pipeline", "/api/pipeline/delete", "/api/buyer",
               "/api/roll/import", "/api/jobs"} | OWNER_POST

# Slow routes run as background jobs: the browser polls for the answer, so a
# minutes-long scan survives the tunnel's 100-second request limit.
JOB_ROUTES = {"/api/search": "_search", "/api/leads": "_leads", "/api/analyze": "_analyze",
              "/api/dossier": "_dossier"}
JOB_TTL = 3600
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

FORWARD_HEADERS = ("Cf-Connecting-Ip", "Cf-Ray", "X-Forwarded-For", "X-Forwarded-Host", "Forwarded")
TOKEN_IN_PATH = re.compile(r"(/join/)[A-Za-z0-9_-]+")

DENIED_PAGE = b"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>FlipComp</title>
<style>body{margin:0;background:#0f1115;color:#e8eaed;font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif;
display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px}
div{max-width:420px;background:#171a21;border:1px solid #2a2f3a;border-radius:12px;padding:24px}
h1{font-size:20px;margin:0 0 8px}p{color:#9aa3b2;margin:0}</style></head><body><div>
<h1>FlipComp is private</h1><p>You need a personal invite link to use it. Ask the person who runs it
to send you one. If yours stopped working, it has probably been replaced, so ask for the new one.</p>
</div></body></html>"""


class _Capture:
    """Stands in for a request handler so a route can run as a background job."""

    def __init__(self, user):
        self.user = user
        self.result = (500, {"error": "No result."})

    def _json(self, code, payload):
        self.result = (code, json.loads(json.dumps(payload, default=str)))


def _prune_jobs():
    cutoff = time.time() - JOB_TTL
    with _jobs_lock:
        for jid in [j for j, v in _jobs.items() if v["created"] < cutoff]:
            del _jobs[jid]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    user: dict | None = None

    def log_message(self, fmt, *args):  # quieter console; never print invite tokens
        sys.stderr.write("  %s\n" % TOKEN_IN_PATH.sub(r"\1...", fmt % args))

    # --- helpers --------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict):
        self._send(code, json.dumps(payload, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _file(self, relpath: str):
        path = os.path.normpath(os.path.join(STATIC, relpath.lstrip("/")))
        if not path.startswith(STATIC) or not os.path.isfile(path):
            return self._send(404, b"Not found", "text/plain")
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
        }.get(os.path.splitext(path)[1], "application/octet-stream")
        with open(path, "rb") as fh:
            self._send(200, fh.read(), ctype)

    # --- who is asking ---------------------------------------------------
    def _is_local(self) -> bool:
        """A request made on this computer, not relayed by the tunnel."""
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return False
        return not any(self.headers.get(h) for h in FORWARD_HEADERS)

    def _authenticate(self):
        if self._is_local():
            return access.owner()
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie") or "")
        except Exception:
            pass
        morsel = cookie.get(access.COOKIE)
        user = access.user_for_token(morsel.value if morsel else None)
        if user:
            access.touch(user["id"])
        return user

    def _deny(self, path: str):
        if path.startswith("/api/") or path.startswith("/letters"):
            return self._json(401, {"error": "Not signed in. Open your personal invite link again."})
        return self._send(403, DENIED_PAGE, "text/html; charset=utf-8")

    def _is_owner(self) -> bool:
        return bool(self.user) and self.user.get("role") == "owner"

    def _by(self) -> str | None:
        """Name for the pipeline log, once more than one person uses the app."""
        if len(access.list_users()) < 2:
            return None
        return (self.user or {}).get("name")

    def _buyer(self) -> dict:
        """Partners sign letters with their own details; the owner uses prefs."""
        if self.user and self.user.get("role") != "owner":
            return {k: self.user.get(k, "") for k in prefs.BUYER_KEYS}
        return prefs.load_buyer()

    def _join(self, path: str):
        token = path[len("/join/"):].strip("/")
        user = access.user_for_token(token)
        if not user:
            return self._send(403, DENIED_PAGE, "text/html; charset=utf-8")
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        nxt = (qs.get("next") or ["/"])[0]
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        https = (self.headers.get("X-Forwarded-Proto") == "https"
                 or "https" in (self.headers.get("Cf-Visitor") or ""))
        cookie = (f"{access.COOKIE}={token}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax"
                  + ("; Secure" if https else ""))
        access.touch(user["id"])
        self.send_response(302)
        self.send_header("Set-Cookie", cookie)
        self.send_header("Location", nxt)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

    # --- routes ---------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/join/"):
            return self._join(path)
        self.user = self._authenticate()
        if not self.user:
            return self._deny(path)
        if path in OWNER_GET and not self._is_owner():
            return self._json(403, {"error": "Only the owner can do that."})

        if path in ("/", "/index.html"):
            return self._file("index.html")
        if path == "/api/me":
            return self._json(200, {"name": self.user.get("name"), "role": self.user.get("role"),
                                    "local": self._is_local(), "team_size": len(access.list_users())})
        if path.startswith("/api/jobs/"):
            job = _jobs.get(path.rsplit("/", 1)[-1])
            if not job or job["user"] != self.user["id"]:
                return self._json(404, {"error": "That request expired. Run it again."})
            if not job["done"]:
                return self._json(200, {"done": False, "elapsed": round(time.time() - job["created"])})
            code, payload = job["result"]
            return self._json(200, {"done": True, "status": code, "result": payload})
        if path == "/report":
            fp = os.path.join(report.REPORT_DIR, "latest.html")
            if not os.path.isfile(fp):
                return self._send(404, b"No daily report yet.", "text/plain")
            with open(fp, "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        if path == "/api/team":
            return self._json(200, self._team_state())
        if path == "/api/whatsnew":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                days = max(1, min(int((qs.get("days") or ["7"])[0]), 90))
            except ValueError:
                days = 7
            return self._json(200, {"days": days,
                                    "leads": history.since(days, source="leads"),
                                    "deals": history.since(days, source="deals")})
        if path == "/api/prefs":
            return self._json(200, prefs.load())
        if path == "/api/pipeline":
            return self._json(200, {"deals": pipeline.list_deals(), "summary": pipeline.summary(),
                                    "due": pipeline.due(), "buyer": self._buyer()})
        if path in ("/letters", "/api/mailers.csv"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            src = (qs.get("source") or ["pipeline"])[0]
            focus_only = (qs.get("focus") or ["0"])[0] == "1"
            if src in ("tax", "roll", "offmarket"):
                try:
                    n = max(1, min(int((qs.get("n") or ["50"])[0]), 500))
                except ValueError:
                    n = 50
                kinds = {"tax": ("tax",), "roll": ("roll",), "offmarket": ("tax", "roll")}[src]
                rows = history.top_tax_leads(n, (qs.get("county") or [None])[0], kinds, focus_only)
            else:
                ids = set(",".join(qs.get("ids") or []).split(",")) - {""}
                rows = [d for d in pipeline.list_deals(False) if not ids or d["id"] in ids]
            if path == "/letters":
                return self._send(200, letters.letters_document(rows, self._buyer()).encode("utf-8"),
                                  "text/html; charset=utf-8")
            body = letters.mail_merge_csv(rows).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=mailers.csv")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/config":
            return self._json(200, {
                "tiers": {k: {"label": v["label"], "psf": v["psf"], "desc": v["desc"]}
                          for k, v in rehab.TIERS.items()},
                "line_items": {k: {"label": v["label"]} for k, v in rehab.LINE_ITEMS.items()},
                "defaults": offer.DEFAULTS,
                "default_contingency_pct": round(rehab.DEFAULT_CONTINGENCY * 100),
                "counties": {k: {"name": v["name"], "seat": v["seat"]}
                             for k, v in sorted(regions.COUNTIES.items())},
                "default_county": regions.DEFAULT_REGION,
                "large_metro": sorted(regions.LARGE_METRO),
            })
        return self._file(path)

    # --- slow routes (run directly, or as jobs) ----------------------------
    def _search(self, payload: dict):
        """Regional deal scan. Slow by nature -- it reads a whole region."""
        filters = {}
        for key in ("min_price", "max_price", "min_sqft", "max_sqft",
                    "min_spread", "max_results"):
            val = payload.get(key)
            if val not in (None, ""):
                try:
                    filters[key] = float(val)
                except (TypeError, ValueError):
                    pass
        if "max_results" in filters:
            filters["max_results"] = int(filters["max_results"])

        try:
            verify_top = int(payload.get("verify_top") or 8)
        except (TypeError, ValueError):
            verify_top = 8

        try:
            result = search.find_deals(
                home=(payload.get("county") or regions.DEFAULT_REGION),
                include_adjacent=bool(payload.get("include_adjacent", True)),
                include_metro=bool(payload.get("include_metro", False)),
                filters=filters,
                verify_top=max(0, min(verify_top, 25)),
            )
        except KeyError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            return self._json(500, {"error": f"Search failed: {exc}"})
        try:
            cands = [{**c, "kind": "deal", "addr_key": leads._addr_key(c.get("address"))}
                     for c in result["candidates"]]
            for v in result.get("verified") or []:
                for c in cands:
                    if c["address"] == v.get("address") and v.get("verified"):
                        c.update({"verdict": v["verdict"], "mao": v["mao"]})
            result["diff"] = history.record(cands, "deals")
        except Exception:
            traceback.print_exc()
            result["diff"] = None
        return self._json(200, result)

    def _leads(self, payload: dict):
        """Off-market lead discovery across the region."""
        try:
            tax_details = int(payload.get("tax_details") or 250)
        except (TypeError, ValueError):
            tax_details = 250
        try:
            result = leads.find_leads(
                home=(payload.get("county") or regions.DEFAULT_REGION),
                include_adjacent=bool(payload.get("include_adjacent", True)),
                include_metro=bool(payload.get("include_metro", False)),
                tax_details=max(0, min(tax_details, 1500)),
            )
        except KeyError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            return self._json(500, {"error": f"Lead search failed: {exc}"})
        try:
            result["diff"] = history.record(
                result["listed"] + result["expired"] + result["tax"], "leads",
                stats=result["stats"])
        except Exception:
            traceback.print_exc()
            result["diff"] = None
        return self._json(200, result)

    def _dossier(self, payload: dict):
        """Public-record dossier for one address."""
        address = (payload.get("address") or "").strip()
        if not address:
            return self._json(400, {"error": "Enter an address."})
        county = (payload.get("county") or "").strip().lower() or None
        try:
            return self._json(200, dossier.build(address, county))
        except Exception as exc:
            traceback.print_exc()
            return self._json(500, {"error": f"Lookup failed: {exc}"})

    def _analyze(self, payload: dict):
        address = (payload.get("address") or "").strip()
        if not address:
            return self._json(400, {"error": "Enter a property address."})

        asking = payload.get("asking_price")
        try:
            asking = float(asking) if asking not in (None, "") else None
        except (TypeError, ValueError):
            asking = None

        raw = payload.get("overrides") or {}
        overrides: dict = {}
        for key, val in raw.items():
            if val in (None, ""):
                continue
            if key in NUMERIC_OVERRIDES:
                try:
                    overrides[key] = float(val)
                except (TypeError, ValueError):
                    continue
            elif key in ("rehab_tier",):
                overrides[key] = str(val)
            elif key == "rehab_line_items":
                overrides[key] = [str(x) for x in val] if isinstance(val, list) else []
            elif key == "cash_purchase":
                overrides[key] = bool(val)

        try:
            result = analyze(address, asking_price=asking, overrides=overrides)
        except (DataError, CompError) as exc:
            return self._json(422, {"error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            return self._json(500, {"error": f"Unexpected failure: {exc}"})
        return self._json(200, result)

    def _start_job(self, payload: dict):
        route = payload.get("route")
        if route not in JOB_ROUTES:
            return self._json(400, {"error": "Unknown job."})
        _prune_jobs()
        jid = uuid.uuid4().hex
        job = {"user": self.user["id"], "created": time.time(), "done": False, "result": None}
        with _jobs_lock:
            _jobs[jid] = job
        cap = _Capture(self.user)
        method = getattr(Handler, JOB_ROUTES[route])
        body = payload.get("payload") or {}

        def run():
            try:
                method(cap, body)
            except Exception as exc:
                traceback.print_exc()
                cap.result = (500, {"error": f"Failed: {exc}"})
            job["result"] = cap.result
            job["done"] = True

        threading.Thread(target=run, daemon=True).start()
        return self._json(200, {"id": jid})

    # --- quick routes ---------------------------------------------------
    def _leads_oscn(self, payload: dict):
        """Classify a pasted OSCN results page and match defendants to parcels."""
        html = payload.get("html") or ""
        if len(html.strip()) < 50:
            return self._json(400, {"error": "Paste the saved OSCN page source first."})
        county = (payload.get("county") or regions.DEFAULT_REGION).strip().lower()
        try:
            return self._json(200, leads.oscn_from_html(county, html))
        except Exception as exc:
            traceback.print_exc()
            return self._json(500, {"error": f"Could not parse: {exc}"})

    def _prefs(self, payload: dict):
        """Replace the list of places to skip; purge them from history."""
        places = payload.get("exclude") or []
        if isinstance(places, str):
            places = [p for p in places.replace(";", ",").split(",")]
        saved = prefs.set_excluded_places([str(p) for p in places], set(regions.COUNTIES))
        try:
            saved["purged"] = history.purge_excluded()
        except Exception:
            saved["purged"] = 0
        return self._json(200, saved)

    def _taxcheck(self, payload: dict):
        """Delinquency lookup for one address."""
        address = (payload.get("address") or "").strip()
        county = (payload.get("county") or regions.DEFAULT_REGION).strip().lower()
        if not address:
            return self._json(400, {"error": "Enter an address."})
        try:
            return self._json(200, taxroll.delinquency_for_address(county, address))
        except Exception as exc:
            return self._json(500, {"error": f"Tax roll lookup failed: {exc}"})

    def _save_buyer(self, payload: dict):
        fields = {k: payload.get(k) for k in prefs.BUYER_KEYS}
        if self.user and self.user.get("role") != "owner":
            # Partners keep their own letter signature on their team record.
            self.user = access.save_buyer(self.user["id"], fields)
            return self._json(200, self._buyer())
        return self._json(200, prefs.save_buyer(**fields))

    # --- team (owner only) ------------------------------------------------
    def _team_state(self) -> dict:
        return {"users": [{**{k: v for k, v in u.items() if k != "token"}, "link": access.join_link(u)}
                          for u in access.list_users()],
                "settings": access.settings_public(), "base_url": access.base_url(),
                "tunnel": tunnel.status(), "mail_ready": mailer.configured()}

    def _team(self, route: str, payload: dict):
        try:
            if route == "/api/team/add":
                access.add(payload.get("name") or "", payload.get("email") or "",
                           bool(payload.get("daily_email", True)))
            elif route == "/api/team/update":
                access.update(str(payload.get("id") or ""),
                              **{k: payload.get(k) for k in access.USER_FIELDS})
            elif route == "/api/team/link":
                access.new_link(str(payload.get("id") or ""))
            elif route == "/api/team/remove":
                access.remove(str(payload.get("id") or ""))
            elif route == "/api/team/settings":
                access.save_settings(**{k: payload.get(k) for k in access.SETTING_FIELDS + ("clear",)})
            elif route == "/api/team/invite":
                u = access.get(str(payload.get("id") or ""))
                if not u:
                    return self._json(404, {"error": "No such person."})
                return self._json(200, {**mailer.send_invite(u), "team": self._team_state()})
            elif route == "/api/team/send-daily":
                only = [str(payload["id"])] if payload.get("id") else None
                res = mailer.send_daily(history.since(1, source="leads"),
                                        history.since(1, source="deals"), only=only)
                return self._json(200, {**res, "team": self._team_state()})
            elif route == "/api/team/tunnel":
                if payload.get("action") == "stop":
                    tunnel.stop()
                else:
                    tunnel.stop()
                    tunnel.start(PORT)
                    time.sleep(6)  # long enough for a quick tunnel to report its address
        except (ValueError, KeyError) as exc:
            return self._json(400, {"error": str(exc).strip("'")})
        except mailer.MailError as exc:
            return self._json(422, {"error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            return self._json(500, {"error": f"Failed: {exc}"})
        return self._json(200, self._team_state())

    def do_POST(self):
        route = self.path.split("?")[0]
        self.user = self._authenticate()
        if not self.user:
            return self._deny(route)
        if route not in POST_ROUTES:
            return self._send(404, b"Not found", "text/plain")
        if route in OWNER_POST and not self._is_owner():
            return self._json(403, {"error": "Only the owner can do that."})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json(400, {"error": "Malformed request."})

        if route == "/api/jobs":
            return self._start_job(payload)
        if route in JOB_ROUTES:
            return getattr(self, JOB_ROUTES[route])(payload)
        if route.startswith("/api/team/"):
            return self._team(route, payload)
        if route == "/api/leads/oscn":
            return self._leads_oscn(payload)
        if route == "/api/prefs":
            return self._prefs(payload)
        if route == "/api/pipeline":
            try:
                return self._json(200, pipeline.upsert(payload, by=self._by()))
            except Exception as exc:
                traceback.print_exc()
                return self._json(500, {"error": f"Could not save: {exc}"})
        if route == "/api/pipeline/delete":
            return self._json(200, {"deleted": pipeline.delete(str(payload.get("id") or ""))})
        if route == "/api/roll/import":
            try:
                raw = base64.b64decode(payload.get("content_b64") or "")
                res = roll_import.import_roll(raw, payload.get("filename") or "",
                                              (payload.get("county") or "").lower() or None)
                if res.get("leads"):
                    res["diff"] = history.record(res["leads"], "roll")
                res["leads"] = res.get("leads", [])[:300]
                return self._json(200 if not res.get("error") else 422, res)
            except Exception as exc:
                traceback.print_exc()
                return self._json(500, {"error": f"Import failed: {exc}"})
        if route == "/api/buyer":
            return self._save_buyer(payload)
        return self._taxcheck(payload)


def main():
    ap = argparse.ArgumentParser(description="FlipComp web server")
    ap.add_argument("--share", action="store_true",
                    help="also open a Cloudflare Tunnel so invited partners can use the app")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    args = ap.parse_args()

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"\n  FlipComp running at {url}")
    access.owner()
    if args.share:
        st = tunnel.start(PORT)
        if st.get("error"):
            print(f"  Sharing is OFF: {st['error']}")
        else:
            print("  Opening the tunnel; the shared address appears here in a few seconds.")
    print("  Press Ctrl+C to stop.\n")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        tunnel.stop()
        srv.server_close()


if __name__ == "__main__":
    main()
