"""studio2 static-server plugin (loaded via STUDIO2_PLUGINS, like Van Hunter's): forwards
/airq/api/* to the Board-AirQ live API on 127.0.0.1:8142 (board-airq-api.service), so the
board stays same-origin and the HA token / control PIN never leave the service env.

  GET  /airq/api/data, /airq/api/status  -> read
  POST /airq/api/set                      -> PIN-gated level control (body passed through)
"""
import urllib.request, urllib.error
UPSTREAM = "http://127.0.0.1:8142"


def _up(path):
    # board mounts at /airq/; the API service exposes /api/* -> strip the mount prefix
    return UPSTREAM + (path[len("/airq"):] if path.startswith("/airq/") else path)


def _write(h, code, body):
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Cache-Control", "no-store")
    h.send_header("CDN-Cache-Control", "no-store")
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


def setup(root):
    def fwd_get(h):
        try:
            r = urllib.request.urlopen(_up(h.path), timeout=20)
            code, body = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, body = e.code, e.read()
        except Exception as e:
            code = 502
            body = ('{"error":"board-airq api unavailable","detail":"%s"}'
                    % str(e)[:80].replace('"', "'")).encode()
        _write(h, code, body)

    def fwd_set(h):
        try:
            n = int(h.headers.get("Content-Length") or 0)
            data = h.rfile.read(n) if 0 < n <= 4096 else b"{}"
            req = urllib.request.Request(_up(h.path), data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            r = urllib.request.urlopen(req, timeout=20)
            code, body = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, body = e.code, e.read()
        except Exception as e:
            code = 502
            body = ('{"error":"board-airq api unavailable","detail":"%s"}'
                    % str(e)[:80].replace('"', "'")).encode()
        _write(h, code, body)

    routes = {("GET", "/airq/api/" + k): fwd_get for k in ("data", "status")}
    routes[("POST", "/airq/api/set")] = fwd_set
    return routes
