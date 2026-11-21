#!/usr/bin/env python3
"""Board-AirQ live data API (runs on Domvs, loopback). Reads Home Assistant locally
and serves the air-quality-vs-purifiers data the board renders.

  GET /api/data    -> {at, current:{cadr,cadr_max,indoor,outdoor}, units, series:{cadr,indoor,outdoor}}
  GET /api/status  -> {ok, at, ha, sensors}

Sensors (same ones the HA ApexCharts card uses):
  sensor.purifiers_clean_air_rate   capacity in use (m3/h, 0..720 combined CADR of the 3 Core 300S)
  sensor.indoor_air_quality_pm2_5   PM2.5 interna (ug/m3)
  sensor.dubai_outdoor_air_quality  PM2.5 esterna (ug/m3)

The studio2 static server forwards /airq/api/* to this service (api/studio2_plugin.py,
loaded via STUDIO2_PLUGINS) so the board stays same-origin. HA_TOKEN lives only in the
systemd unit env, never in git.
"""
import json, os, time, threading, datetime, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HA = os.environ.get("HA_URL", "http://127.0.0.1:8123")
TOKEN = os.environ.get("HA_TOKEN", "")
PORT = int(os.environ.get("AIRQ_PORT", "8142"))
HOURS = int(os.environ.get("AIRQ_HOURS", "24"))
CADR_MAX = float(os.environ.get("AIRQ_CADR_MAX", "720"))
CACHE_TTL = 60  # seconds

CADR = "sensor.purifiers_clean_air_rate"
INDOOR = "sensor.indoor_air_quality_pm2_5"
OUTDOOR = "sensor.dubai_outdoor_air_quality"
ENTS = [CADR, INDOOR, OUTDOOR]

_lock = threading.Lock()
_cache = {"at": 0, "body": None}


def _ha(path):
    req = urllib.request.Request(HA + path, headers={
        "Authorization": "Bearer %s" % TOKEN, "User-Agent": "Board-AirQ/1.0"})
    with urllib.request.urlopen(req, timeout=12) as f:
        return json.load(f)


def _num(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _ts_ms(s):
    # HA timestamps: 2026-11-20T01:27:55+00:00
    try:
        return int(datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def _state(entity):
    s = _ha("/api/states/" + entity)
    return _num(s.get("state"))


def _history():
    start = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=HOURS)) \
        .strftime("%Y-%m-%dT%H:%M:%S")
    raw = _ha("/api/history/period/%s?filter_entity_id=%s&minimal_response&no_attributes"
              % (start, ",".join(ENTS)))
    by = {CADR: [], INDOOR: [], OUTDOOR: []}
    for group in raw:
        if not group:
            continue
        eid = group[0].get("entity_id")
        if eid not in by:
            continue
        for p in group:
            v = _num(p.get("state"))
            if v is None:  # unavailable / unknown
                continue
            t = _ts_ms(p.get("last_changed") or p.get("last_updated") or "")
            if t is not None:
                by[eid].append([t, v])
    return by


def build():
    hist = _history()
    cur = {}
    for key, ent in (("cadr", CADR), ("indoor", INDOOR), ("outdoor", OUTDOOR)):
        v = _state(ent)
        if v is None and hist[ent]:
            v = hist[ent][-1][1]
        cur[key] = v
    cur["cadr_max"] = CADR_MAX
    out = {
        "at": int(time.time()),
        "current": cur,
        "units": {"cadr": "m\u00b3/h", "pm": "\u00b5g/m\u00b3"},
        "series": {"cadr": hist[CADR], "indoor": hist[INDOOR], "outdoor": hist[OUTDOOR]},
    }
    return json.dumps(out, ensure_ascii=False).encode()


def cached_body():
    now = time.time()
    with _lock:
        if _cache["body"] is not None and now - _cache["at"] < CACHE_TTL:
            return _cache["body"]
    body = build()
    with _lock:
        _cache["body"] = body
        _cache["at"] = now
    return body


class H(BaseHTTPRequestHandler):
    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/api/status":
            ok, detail = True, "ok"
            try:
                _ha("/api/")
            except Exception as e:
                ok, detail = False, str(e)[:120]
            return self._send(200 if ok else 503,
                              json.dumps({"ok": ok, "at": int(time.time()), "ha": HA,
                                          "detail": detail, "sensors": ENTS}).encode())
        if path not in ("/api/data", "/api", "", "/"):
            return self._send(404, b'{"error":"not found"}')
        try:
            body = cached_body()
        except Exception as e:
            with _lock:
                body = _cache["body"]
            if body is None:
                return self._send(503, ('{"error":"ha unavailable","detail":"%s"}'
                                        % str(e)[:120].replace('"', "'")).encode())
        return self._send(200, body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
