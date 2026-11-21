"""studio2 static-server plugin (loaded via STUDIO2_PLUGINS, like Van Hunter's): forwards
GET /airq/api/{data,status} to the Board-AirQ live API on 127.0.0.1:8142 (board-airq-api.service),
so the board stays same-origin and the HA token never leaves the service env.
"""
import urllib.request, urllib.error
UPSTREAM = "http://127.0.0.1:8142"


def setup(root):
    def fwd(h):
        # board mounts at /airq/; the API service exposes /api/*  -> strip the mount prefix
        up = UPSTREAM + (h.path[len("/airq"):] if h.path.startswith("/airq/") else h.path)
        try:
            r = urllib.request.urlopen(up, timeout=20)
            code, body = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, body = e.code, e.read()
        except Exception as e:
            code = 502
            body = ('{"error":"board-airq api unavailable","detail":"%s"}'
                    % str(e)[:80].replace('"', "'")).encode()
        h.send_response(code)
        h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("Cache-Control", "no-store")
        h.send_header("CDN-Cache-Control", "no-store")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
    return {("GET", "/airq/api/" + k): fwd for k in ("data", "status")}
