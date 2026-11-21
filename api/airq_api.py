#!/usr/bin/env python3
"""Board-AirQ live data API (runs on Domvs, loopback). Reads Home Assistant locally
and serves the air-quality-vs-purifiers data the board renders, plus a PIN-gated
control to set the purifier level (same entity as the HA "Cycle" / "Livello manuale"
dropdowns: input_select.air_purifier_cycle).

  GET  /api/data?from=<ms>&to=<ms>  -> {at, window, current:{cadr,cadr_max,indoor,outdoor,cycle,control},
                                        states:[...], units, series:{cadr,indoor,outdoor}}
  GET  /api/status                  -> {ok, at, ha, sensors}
  POST /api/set  {pin, state}       -> set the fan level (state in off|low|med|ultra). 403 without a valid PIN.

Sensors / entity:
  sensor.purifiers_clean_air_rate   capacity in use (m3/h, 0..720 combined CADR of the 3 Core 300S)
  sensor.indoor_air_quality_pm2_5   indoor PM2.5 (ug/m3)
  sensor.dubai_outdoor_air_quality  outdoor PM2.5 (ug/m3)
  input_select.air_purifier_cycle   level selector (Off/Min/Med/Max/Ultra)

The 4 board states map to a cycle option and its rated suction power (combined CADR):
  off=Off(0)  low=Min(180)  med=Med(411)  ultra=Ultra(720)

Writes are gated by AIRQ_PIN (env). No PIN set -> control disabled, reads still work.
HA_TOKEN / AIRQ_PIN live only in the systemd unit env, never in git. The studio2 server
forwards /airq/api/* here so the board stays same-origin.
"""
import json, os, time, hmac, threading, datetime, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HA = os.environ.get("HA_URL", "http://127.0.0.1:8123")
TOKEN = os.environ.get("HA_TOKEN", "")
PIN = os.environ.get("AIRQ_PIN", "").strip()
PORT = int(os.environ.get("AIRQ_PORT", "8142"))
CADR_MAX = float(os.environ.get("AIRQ_CADR_MAX", "720"))
MAXPTS = int(os.environ.get("AIRQ_MAXPTS", "2500"))
CACHE_TTL = 60
MAX_SPAN_MS = 366 * 24 * 3600 * 1000

CADR = "sensor.purifiers_clean_air_rate"
INDOOR = "sensor.indoor_air_quality_pm2_5"
OUTDOOR = "sensor.dubai_outdoor_air_quality"
CYCLE = "input_select.air_purifier_cycle"
ENTS = [CADR, INDOOR, OUTDOOR]

# board state -> (cycle option, rated combined suction power m3/h)
STATES = [("off", "Off", 0), ("low", "Min", 180), ("med", "Med", 411), ("ultra", "Ultra", 720)]
STATE_BY_KEY = {k: (opt, pw) for k, opt, pw in STATES}

_lock = threading.Lock()
_cache = {}            # key -> (at, body)
_attempts = []         # timestamps of failed PIN attempts (global rate limit)


def _ha(path):
    req = urllib.request.Request(HA + path, headers={
        "Authorization": "Bearer %s" % TOKEN, "User-Agent": "Board-AirQ/1.0"})
    with urllib.request.urlopen(req, timeout=20) as f:
        return json.load(f)


def _ha_post(path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(HA + path, data=data, method="POST", headers={
        "Authorization": "Bearer %s" % TOKEN, "Content-Type": "application/json",
        "User-Agent": "Board-AirQ/1.0"})
    with urllib.request.urlopen(req, timeout=15) as f:
        return f.read()


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
    return _ha("/api/states/" + entity).get("state")


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
        v = _num(_state(ent))
        if v is None and hist[ent]:
            v = hist[ent][-1][1]
        cur[key] = v
    try:
        cur["cycle"] = _state(CYCLE)
    except Exception:
        cur["cycle"] = None
    cur["cadr_max"] = CADR_MAX
    cur["control"] = bool(PIN)
    out = {
        "at": int(time.time()),
        "window": {"from": frm, "to": to},
        "current": cur,
        "states": [{"key": k, "option": opt, "power": pw} for k, opt, pw in STATES],
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
        if len(_cache) > 64:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:32]:
                _cache.pop(k, None)
    return body


def _locked():
    now = time.time()
    with _lock:
        recent = [t for t in _attempts if now - t < 300]
        _attempts[:] = recent
        return len(recent) >= 8  # >=8 wrong PIN tries in 5 min -> locked


def _fail():
    with _lock:
        _attempts.append(time.time())


def do_set(req):
    if not PIN:
        return 503, {"error": "control disabled"}
    if _locked():
        return 429, {"error": "too many attempts, wait a few minutes"}
    pin = str(req.get("pin") or "")
    if not hmac.compare_digest(pin.encode(), PIN.encode()):
        _fail()
        return 403, {"error": "wrong pin"}
    st = str(req.get("state") or "").lower()
    if st not in STATE_BY_KEY:
        return 400, {"error": "bad state"}
    option = STATE_BY_KEY[st][0]
    try:
        _ha_post("/api/services/input_select/select_option",
                 {"entity_id": CYCLE, "option": option})
    except Exception as e:
        return 502, {"error": "ha set failed", "detail": str(e)[:120]}
    with _lock:
        _cache.clear()  # force fresh read on next /api/data
    return 200, {"ok": True, "cycle": option, "state": st}


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        if path != "/api/set":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}") if 0 < n <= 4096 else {}
        except Exception:
            return self._send(400, {"error": "bad request"})
        code, obj = do_set(req)
        return self._send(code, obj)

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
                              {"ok": ok, "at": int(time.time()), "ha": HA, "detail": detail,
                               "sensors": ENTS, "control": bool(PIN)})
        if path not in ("/api/data", "/api", ""):
            return self._send(404, {"error": "not found"})
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
            return self._send(503, {"error": "ha unavailable", "detail": str(e)[:120]})
        return self._send(200, body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
