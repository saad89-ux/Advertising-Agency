#!/usr/bin/env python3
"""Serves the SYNTHETIC storefronts on http://localhost:8099/<client>/<path>. Stdlib only.
302 pages DROP the query string (to test UTM survival). Any POST is logged to form_submissions.log."""
import json, pathlib, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
BASE = pathlib.Path(__file__).parent
PAGES = json.loads((BASE / "pages_index.json").read_text())["pages"]
LOOKUP = {(p["client"], p["path"]): p for p in PAGES}
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _find(self):
        parts = urlparse(self.path).path.strip("/").split("/", 1)
        if len(parts) != 2: return None, None
        return parts[0], "/" + parts[1]
    def do_GET(self):
        c, path = self._find()
        p = LOOKUP.get((c, path))
        if p is None and (c, path) != (None, None):
            f = BASE / c / (path.strip("/") + ".html")
            if f.exists(): return self._send(200, f.read_bytes())
        if p is None: return self._send(404, b"not found")
        if p["status"] == 404: return self._send(404, b"not found")
        if p["status"] == 302:
            self.send_response(302); self.send_header("Location", f"/{c}{p['redirect_to']}"); self.end_headers(); return
        self._send(200, (BASE.parent / p["file"]).read_bytes())
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); self.rfile.read(n)
        with open(BASE / "form_submissions.log", "a") as f:
            f.write(f"{datetime.datetime.utcnow().isoformat()} POST {self.path}\n")
        self._send(200, b"submitted")
    def _send(self, code, body):
        self.send_response(code); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(body)
if __name__ == "__main__":
    print("Serving on http://localhost:8099"); HTTPServer(("127.0.0.1", 8099), H).serve_forever()
