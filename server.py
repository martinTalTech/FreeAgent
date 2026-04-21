"""
TalTech Aineplanner — lokaalne proxy server
===========================================
Käivita:  python server.py
Ava:      http://localhost:8080

See server:
1. Serveerib index.html brauserile
2. Edastab /api/* päringud TalTechi API-le (lahendab CORS probleemi)
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request, urllib.error, json, os, sys

PORT      = 8080
API_BASE  = "https://tunniplaan-api.taltech.ee/api"
API_HDRS  = {
    "Origin":       "https://tunniplaan.taltech.ee",
    "Referer":      "https://tunniplaan.taltech.ee/",
    "Accept":       "application/json",
    "Content-Type": "application/json",
}

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"  {self.command} {self.path} → {args[1] if len(args)>1 else ''}")

    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_file("index.html", "text/html; charset=utf-8")
        elif self.path.startswith("/proxy/"):
            # /proxy/public/departments?ttId=10  →  API_BASE/public/departments?ttId=10
            api_path = self.path[len("/proxy"):]
            self._proxy_get(api_path)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path.startswith("/proxy/"):
            api_path  = self.path[len("/proxy"):]
            length    = int(self.headers.get("Content-Length", 0))
            body      = self.rfile.read(length)
            self._proxy_post(api_path, body)
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_file(self, filename, ctype):
        try:
            path = os.path.join(os.path.dirname(__file__), filename)
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_cors()
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def _proxy_get(self, api_path):
        url = API_BASE + api_path
        req = urllib.request.Request(url, headers=API_HDRS, method="GET")
        self._do_proxy(req)

    def _proxy_post(self, api_path, body):
        url = API_BASE + api_path
        req = urllib.request.Request(url, data=body, headers=API_HDRS, method="POST")
        self._do_proxy(req)

    def _do_proxy(self, req):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_cors()
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_cors()
            self.end_headers()
            self.wfile.write(e.read())
        except Exception as e:
            self.send_response(502)
            self.send_cors()
            self.end_headers()
            self.wfile.write(str(e).encode())

if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    os.chdir(here)

    print(f"\n{'='*50}")
    print(f"  TalTech Aineplanner")
    print(f"{'='*50}")
    print(f"  Server käivitub pordil {PORT}...")
    print(f"  Ava brauseris: http://localhost:{PORT}")
    print(f"  Peata: Ctrl+C")
    print(f"{'='*50}\n")

    server = HTTPServer(("localhost", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server peatatud.")
