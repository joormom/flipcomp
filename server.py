"""FlipComp local web server.

Deliberately stdlib-only: this is a single-user local tool with three endpoints,
so a framework would add dependency risk without buying anything. Run it with
`python server.py` and open the URL it prints.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from flipcomp import dossier, history, leads, offer, prefs, regions, rehab, search, taxroll
from flipcomp.analyze import analyze
from flipcomp.compengine import CompError
from flipcomp.data import DataError

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
PORT = int(os.environ.get("FLIPCOMP_PORT", "8777"))

NUMERIC_OVERRIDES = {
    "subject_sqft", "subject_beds", "subject_full_baths", "subject_year_built",
    "subject_lot_sqft", "rehab_contingency_pct", "rehab_override_total",
} | set(offer.DEFAULTS)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter console
        sys.stderr.write("  %s\n" % (fmt % args))

    # --- helpers --------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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

    # --- routes ---------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._file("index.html")
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

    def do_POST(self):
        route = self.path.split("?")[0]
        if route in ("/api/search", "/api/leads", "/api/leads/oscn", "/api/taxcheck", "/api/dossier", "/api/prefs"):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._json(400, {"error": "Malformed request."})
            if route == "/api/search":
                return self._search(payload)
            if route == "/api/leads":
                return self._leads(payload)
            if route == "/api/leads/oscn":
                return self._leads_oscn(payload)
            if route == "/api/dossier":
                return self._dossier(payload)
            if route == "/api/prefs":
                return self._prefs(payload)
            return self._taxcheck(payload)

        if route != "/api/analyze":
            return self._send(404, b"Not found", "text/plain")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json(400, {"error": "Malformed request."})

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


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"\n  FlipComp running at {url}")
    print("  Press Ctrl+C to stop.\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        srv.server_close()


if __name__ == "__main__":
    main()
