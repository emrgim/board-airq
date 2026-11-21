#!/usr/bin/env python3
"""Board-AirQ live data API (runs on Domvs, loopback). Reads Home Assistant locally
and serves the air-quality-vs-purifiers data the board renders.

  GET /api/data?from=<ms>&to=<ms>   -> {at, window:{from,to}, current:{cadr,cadr_max,indoor,outdoor},
                                         units, series:{cadr,indoor,outdoor}}
       from/to are epoch milliseconds; default = last 24h (to=now).
  GET /api/status                    -> {ok, at, ha, sensors}

Sensors (same ones the HA ApexCharts card uses):
  sensor.purifiers_clean_air_rate   capacity in use (m3/h, 0..720 combined CADR of the 3 Core 300S)
  sensor.indoor_air_quality_pm2_5   indoor PM2.5 (ug/m3)
  sensor.dubai_outdoor_air_quality  outdoor PM2.5 (ug/m3)

The studio2 static server forwards /airq/api/* to this service (api/studio2_plugin.py,
loaded via STUDIO2_PLUGINS) so the board stays same-origin. HA_TOKEN lives only in the
systemd unit env, never in git.
"""
import json, os, time, threading, datetime, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HA = os.environ.get("HA_URL", "http://127.0.0.1:8123")
TOKEN = os.environ.get("HA_TOKEN", "")
PORT = int(os.environ.get("AIRQ_PORT", "8142"))
CADR_MAX = float(os.environ.get("AIRQ_CADR_MAX", "720"))
MAXPTS = int(os.environ.get("AIRQ_MAXPTS", "2500"))  # per series, downsampled above this
CACHE_TTL = 60
MAX_SPAN_MS = 366 * 24 * 3600 * 1000

CADR = "sensor.purifiers_clean_air_rate"
INDOOR = "sensor.indoor_air_quality_pm2_5"
OUTDOOR = "sensor.dubai_outdoor_air_quality"
ENTS = [CADR, INDOOR, OUTDOOR]

_lock = threading.Lock()
_cache = {}  # key -> (at, body)


def _ha(path):
    req = urllib.request.Request(HA + path, headers={
        "Authorization": "Bearer %s" % TOKEN, "User-Agent": "Board-AirQ/1.0"})
    with urllib.request.urlopen(req, timeout=20) as f:
        return json.load(f)


def _num(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _ts_ms(s):
    try:
        return int(datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def _iso(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _state(entity):
    return _num(_ha("/api/states/" + entity).get("state"))


def _downsample(pts):
    n = len(pts)
    if n <= MAXPTS:
        return pts
    stride = n // MAXPTS + 1
    out = pts[::stride]
    if out[-1] is not pts[-1]:
        out.append(pts[-1])
    return out


def _history(frm, to):
    raw = _ha("/api/history/period/%s?end_time=%s&filter_entity_id=%s&minimal_response&no_attributes"
              % (_iso(frm), urllib.parse.quote(_iso(to)), ",".join(ENTS)))
    by = {CADR: [], INDOOR: [], OUTDOOR: []}
    for group in raw:
        if not group:
            continue
        eid = group[0].get("entity_id")
        if eid not in by:
            continue
        for p in group:
            v = _num(p.get("state"))
            if v is None:
                continue
            t = _ts_ms(p.get("last_changed") or p.get("last_updated") or "")
            if t is not None:
                by[eid].append([t, v])
    return {k: _downsample(v) for k, v in by.items()}


def build(frm, to):
    hist = _history(frm, to)
    cur = {}
    for key, ent in (("cadr", CADR), ("indoor", INDOOR), ("outdoor", OUTDOOR)):
        v = _state(ent)
        if v is None and hist[ent]:
            v = hist[ent][-1][1]
        cur[key] = v
    cur["cadr_max"] = CADR_MAX
    out = {
        "at": int(time.time()),
        "window": {"from": frm, "to": to},
        "current": cur,
        "units": {"cadr": "m\u00b3/h", "pm": "\u00b5g/m\u00b3"},
        "series": {"cadr": hist[CADR], "indoor": hist[INDOOR], "outdoor": hist[OUTDOOR]},
    }
    return json.dumps(out, ensure_ascii=False).encode()


def cached_body(frm, to):
    key = (frm // 60000, to // 60000)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    body = build(frm, to)
    with _lock:
        _cache[key] = (now, body)
        if len(_cache) > 64:  # bound memory
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:32]:
                _cache.pop(k, None)
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
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path.rstrip("/")
        if path == "/api/status":
            ok, detail = True, "ok"
            try:
                _ha("/api/")
            except Exception as e:
                ok, detail = False, str(e)[:120]
            return self._send(200 if ok else 503,
                              json.dumps({"ok": ok, "at": int(time.time()), "ha": HA,
                                          "detail": detail, "sensors": ENTS}).encode())
        if path not in ("/api/data", "/api", ""):
            return self._send(404, b'{"error":"not found"}')
        q = urllib.parse.parse_qs(parts.query)
        now = int(time.time() * 1000)
        try:
            to = int(q.get("to", [now])[0])
        except ValueError:
            to = now
        try:
            frm = int(q.get("from", [to - 24 * 3600 * 1000])[0])
        except ValueError:
            frm = to - 24 * 3600 * 1000
        to = max(min(to, now + 60000), 0)
        frm = max(min(frm, to - 60000), to - MAX_SPAN_MS)
        try:
            body = cached_body(frm, to)
        except Exception as e:
            return self._send(503, ('{"error":"ha unavailable","detail":"%s"}'
                                    % str(e)[:120].replace('"', "'")).encode())
        return self._send(200, body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
