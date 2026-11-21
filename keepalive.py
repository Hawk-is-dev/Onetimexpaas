import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def _self_ping(url: str, every_seconds: int = 600):
    """Render's free web service sleeps after ~15 min without incoming
    traffic. Hitting our own public URL every 10 min keeps it awake."""
    while True:
        time.sleep(every_seconds)
        try:
            urllib.request.urlopen(url, timeout=15).read()
        except Exception:
            pass


def keep_alive():
    """Starts a tiny web server (Render web services must listen on $PORT) and,
    when running on Render, a self-ping thread. Both run in background threads
    and never block the bot."""
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # Render sets RENDER_EXTERNAL_URL automatically on web services.
    public_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("KEEPALIVE_URL")
    if public_url:
        threading.Thread(target=_self_ping, args=(public_url,), daemon=True).start()
