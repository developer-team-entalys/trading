"""
health_check.py — Simple HTTP endpoint for bot liveness probing.
"""
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

log = logging.getLogger(__name__)

_server: HTTPServer | None = None


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # suppress access logs


def start_health_server(port: int = 8080) -> None:
    """Start a lightweight HTTP health-check server in a daemon thread."""
    global _server
    _server = HTTPServer(("0.0.0.0", port), _Handler)
    thread = threading.Thread(target=_server.serve_forever, daemon=True)
    thread.start()
    log.info(f"Health check endpoint listening on :{port}/health")


def stop_health_server() -> None:
    global _server
    if _server:
        _server.shutdown()
        _server = None
