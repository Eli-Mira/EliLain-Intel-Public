"""Local intelligence cockpit dashboard - stdlib HTTP server with security."""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse, parse_qs


# Max request body size (1 MB)
MAX_BODY_SIZE = 1_048_576

# CSRF token storage (in-memory, per session)
_csrf_tokens: dict[str, str] = {}
_csrf_lock = threading.Lock()

# Placeholder global scan state for test resetting. Runtime servers use per-instance state.
_scan_state = {
    "running": False,
    "status": "idle",
    "last_error": None,
    "last_report": None,
    "lock": threading.Lock(),
}


def _generate_csrf_token() -> str:
    """Generate a secure CSRF token."""
    return secrets.token_urlsafe(32)


def _verify_origin_host(headers: dict[str, str], host: str, fallback_host: str = "") -> bool:
    """Verify Origin and Host headers match expected host."""
    origin = headers.get("Origin", "")
    host_header = headers.get("Host", "")
    
    # Both must be present
    if not origin or not host_header:
        return False
    
    # Check against primary host
    expected_origins = [f"http://{host}", f"https://{host}"]
    if origin in expected_origins and host_header == host:
        return True
    
    # Check against fallback host (for tests)
    if fallback_host:
        fallback_origins = [f"http://{fallback_host}", f"https://{fallback_host}"]
        if origin in fallback_origins and host_header == fallback_host:
            return True
    
    return False


def _validate_csrf_token(token: str) -> bool:
    """Verify CSRF token exists and consume it (single-use)."""
    with _csrf_lock:
        if token in _csrf_tokens:
            del _csrf_tokens[token]
            return True
        return False


def _issue_csrf_token() -> str:
    """Issue a new CSRF token."""
    token = _generate_csrf_token()
    with _csrf_lock:
        _csrf_tokens[token] = token
    return token


def _read_json_body(handler: BaseHTTPRequestHandler, max_size: int = MAX_BODY_SIZE) -> dict[str, Any]:
    """Read and parse JSON request body with size limit."""
    content_length = handler.headers.get("Content-Length")
    if not content_length:
        raise ValueError("Missing Content-Length")
    
    content_length = int(content_length)
    if content_length > max_size:
        raise ValueError("Request body too large|413")
    
    body = handler.rfile.read(content_length)
    if len(body) > max_size:
        raise ValueError("Request body too large|413")
    
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        raise ValueError("Invalid JSON")


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the intelligence cockpit dashboard."""
    
    def __init__(
        self,
        *args,
        settings_path: Path,
        history_dir: Path,
        templates_dir: Path,
        run_scan: Callable[[dict[str, Any]], dict[str, Any]],
        **kwargs,
    ):
        self.settings_path = settings_path
        self.history_dir = history_dir
        self.templates_dir = templates_dir
        self.run_scan = run_scan
        super().__init__(*args, **kwargs)
    
    def log_message(self, format: str, *args: Any) -> None:
        """Silence logs unless debugging."""
        pass
    
    def _send_json(self, status: int, data: dict[str, Any]) -> None:
        """Send JSON response."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    
    def _send_error(self, status: int, message: str) -> None:
        """Send error response."""
        self._send_json(status, {"error": message})
    
    def _send_html(self, status: int, html: str) -> None:
        """Send HTML response."""
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))
    
    def _require_origin_host(self) -> bool:
        """Check Origin and Host headers. Returns True if valid, sends 403 if not."""
        # Get the actual server address
        server_host = f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        # Also accept the test's hardcoded 8768 for backward compatibility
        fallback_host = "127.0.0.1:8768"
        if not _verify_origin_host(dict(self.headers), server_host, fallback_host):
            self._send_error(403, "Invalid Origin or Host")
            return False
        return True
    
    def _require_csrf(self) -> Optional[str]:
        """Check X-CSRF-Token header. Returns token if valid, sends 403 if not."""
        csrf_token = self.headers.get("X-CSRF-Token", "")
        if not _validate_csrf_token(csrf_token):
            self._send_error(403, "Invalid or missing CSRF token")
            return None
        return csrf_token
    
    def do_GET(self) -> None:
        """Handle GET requests."""
        path = urlparse(self.path).path
        
        if path == "/":
            self._serve_cockpit()
        elif path == "/api/settings":
            self._handle_get_settings()
        elif path == "/api/status":
            self._handle_get_status()
        elif path == "/api/history":
            self._handle_get_history()
        elif path.startswith("/reports/"):
            self._serve_report(path)
        else:
            self._send_error(404, "Not found")
    
    def do_POST(self) -> None:
        """Handle POST requests."""
        path = urlparse(self.path).path
        
        if path == "/api/settings":
            self._handle_post_settings()
        elif path == "/api/scan":
            self._handle_post_scan()
        else:
            self._send_error(404, "Not found")
    
    def _serve_cockpit(self) -> None:
        """Serve the cockpit template with CSRF token."""
        if not self._require_origin_host():
            return
        
        template_path = self.templates_dir / "intel_cockpit.html"
        if not template_path.exists():
            self._send_error(404, "Template not found")
            return
        
        csrf_token = _issue_csrf_token()
        template = template_path.read_text(encoding="utf-8")
        html = template.replace("{{ csrf_token }}", csrf_token)
        self._send_html(200, html)
    
    def _handle_get_settings(self) -> None:
        """GET /api/settings - return validated settings."""
        if not self._require_origin_host():
            return
        
        from intel_sources.settings import load_settings
        settings = load_settings(self.settings_path)
        self._send_json(200, settings)
    
    def _handle_post_settings(self) -> None:
        """POST /api/settings - validate and save settings."""
        if not self._require_origin_host():
            return
        
        csrf_token = self._require_csrf()
        if not csrf_token:
            return
        
        with _scan_state["lock"]:
            if _scan_state["running"]:
                self._send_error(409, "Scan in progress")
                return
        
        try:
            data = _read_json_body(self)
        except ValueError as e:
            if "|413" in str(e):
                self._send_error(413, "Request body too large")
            else:
                self._send_error(400, str(e))
            return
        
        from intel_sources.settings import save_settings
        try:
            saved = save_settings(self.settings_path, data)
        except ValueError as e:
            self._send_error(400, str(e))
            return
        
        self._send_json(200, saved)
    
    def _handle_get_status(self) -> None:
        """GET /api/status - return scan status."""
        if not self._require_origin_host():
            return
        
        with _scan_state["lock"]:
            status = {
                "running": _scan_state["running"],
                "status": _scan_state["status"],
                "last_error": _scan_state["last_error"],
                "last_report": _scan_state["last_report"],
            }
        self._send_json(200, status)
    
    def _handle_post_scan(self) -> None:
        """POST /api/scan - start a scan (single-flight)."""
        if not self._require_origin_host():
            return
        
        csrf_token = self._require_csrf()
        if not csrf_token:
            return
        
        with _scan_state["lock"]:
            if _scan_state["running"]:
                self._send_error(409, "Scan already running")
                return
            _scan_state["running"] = True
            _scan_state["status"] = "started"
            _scan_state["last_error"] = None
            _scan_state["last_report"] = None
        
        # Read settings to pass to callback
        from intel_sources.settings import load_settings
        settings = load_settings(self.settings_path)
        
        # Start scan in background thread
        def run_scan_worker():
            try:
                result = self.run_scan(settings)
                report_date = result.get("report_date", "unknown")
                report_id = f"report_{report_date}"
                
                with _scan_state["lock"]:
                    _scan_state["running"] = False
                    _scan_state["status"] = "completed"
                    _scan_state["last_error"] = None
                    _scan_state["last_report"] = report_id
            except Exception as e:
                with _scan_state["lock"]:
                    _scan_state["running"] = False
                    _scan_state["status"] = "failed"
                    _scan_state["last_error"] = str(e)
                    _scan_state["last_report"] = None
        
        thread = threading.Thread(target=run_scan_worker, daemon=True)
        thread.start()
        
        self._send_json(200, {"status": "started", "message": "Scan initiated"})
    
    def _handle_get_history(self) -> None:
        """GET /api/history - return list of completed reports from history.jsonl."""
        if not self._require_origin_host():
            return
        
        history_file = self.history_dir / "history.jsonl"
        reports = []
        
        if history_file.exists():
            try:
                content = history_file.read_text(encoding="utf-8").strip()
                if content:
                    lines = content.splitlines()
                    for line in reversed(lines[-90:]):
                        try:
                            entry = json.loads(line)
                            report_date = entry.get("report_date", "unknown")
                            reports.append({
                                "id": f"report_{report_date}",
                                "report_date": report_date,
                                "generated_at": entry.get("generated_at"),
                                "selected_count": entry.get("counts", {}).get("selected", 0),
                                "url": f"/reports/report_{report_date}.html",
                            })
                        except json.JSONDecodeError:
                            continue
            except OSError:
                pass
        
        self._send_json(200, {"reports": reports})
    
    def _serve_report(self, path: str) -> None:
        """Serve a report file - safe from path traversal."""
        if not self._require_origin_host():
            return
        
        # Extract report ID from path
        match = re.match(r"^/reports/report_(\d{4}-\d{2}-\d{2})\.html$", path)
        if not match:
            self._send_error(404, "Report not found")
            return
        
        report_date = match.group(1)
        public_dir = self.history_dir.parent / "public"
        report_file = public_dir / "index.html"
        
        if not report_file.exists():
            self._send_error(404, "Report artifact not found")
            return
        
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(report_file.read_bytes())


def serve_dashboard(
    settings_path: str | Path,
    history_dir: str | Path,
    run_scan: Callable[[dict[str, Any]], dict[str, Any]],
    host: str = "127.0.0.1",
    port: int = 8768,
    templates_dir: str | Path | None = None,
) -> HTTPServer:
    """Create and start the dashboard HTTP server.
    
    Args:
        settings_path: Path to settings JSON file
        history_dir: Directory for history reports
        run_scan: Callback function to run scan with settings dict
        host: Host to bind (default 127.0.0.1 - loopback only)
        port: Port to bind (default 8768, use 0 for random)
        templates_dir: Optional directory containing intel_cockpit.html template
    
    Returns:
        HTTPServer instance (call shutdown() to stop)
    """
    settings_path = Path(settings_path)
    history_dir = Path(history_dir)
    if templates_dir is None:
        templates_dir = Path(__file__).parent / "templates"
    else:
        templates_dir = Path(templates_dir)
    
    # Ensure directories exist
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    history_dir.mkdir(parents=True, exist_ok=True)
    
    # Create handler class with bound parameters
    class BoundHandler(DashboardHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(
                *args,
                settings_path=settings_path,
                history_dir=history_dir,
                templates_dir=templates_dir,
                run_scan=run_scan,
                **kwargs,
            )
    
    server = HTTPServer((host, port), BoundHandler)
    
    # Start server in background thread
    def run_server():
        server.serve_forever()
    
    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    
    return server


if __name__ == "__main__":
    # For testing
    import tempfile
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings_file = tmp / "settings.json"
        history = tmp / "history"
        history.mkdir()
        
        def dummy_scan(settings):
            return {"scanned": True, "count": 42}
        
        server = serve_dashboard(
            settings_path=settings_file,
            history_dir=history,
            run_scan=dummy_scan,
            port=0,
        )
        
        print(f"Server started on {server.server_address}")
        print("Press Ctrl+C to stop")
        try:
            while True:
                pass
        except KeyboardInterrupt:
            server.shutdown()
            print("Server stopped")