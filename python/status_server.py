#!/usr/bin/env python3
"""NetWatch Part B status server: serves latest stored results and logs requests."""

from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_status_payload(results_path: Path) -> tuple[int, str, str]:
    """Return (http_status, content_type, body_text) from results.json."""
    if not results_path.exists():
        msg = "NetWatch Status: no results file found yet (run netwatch.py first).\n"
        return 200, "text/plain; charset=utf-8", msg

    try:
        raw = results_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        records = data.get("results", [])
        if not isinstance(records, list):
            raise ValueError("Invalid results list")
    except (OSError, ValueError, TypeError) as exc:
        return 500, "text/plain; charset=utf-8", f"NetWatch Status Error: {exc}\n"

    generated_at = str(data.get("generated_at", "unknown"))
    open_cnt = sum(1 for item in records if isinstance(item, dict) and item.get("state") == "OPEN")
    closed_cnt = len(records) - open_cnt

    lines = [
        "NetWatch Status Report",
        f"Generated At: {generated_at}",
        f"Total: {len(records)} | OPEN: {open_cnt} | CLOSED: {closed_cnt}",
        "-" * 64,
    ]
    for item in records:
        if not isinstance(item, dict):
            continue
        host = str(item.get("hostname", ""))
        ip = str(item.get("ip_address", ""))
        svc = str(item.get("service", ""))
        port = str(item.get("port", ""))
        state = str(item.get("state", ""))
        rtt = item.get("response_time_ms", "")
        err = str(item.get("error", ""))
        lines.append(
            f"{host:<18} {ip:<15} {svc:<6} {port:<5} {state:<6} rtt={rtt} err={err}"
        )

    return 200, "text/plain; charset=utf-8", "\n".join(lines) + "\n"


def make_handler(results_path: Path, access_log_path: Path) -> type[BaseHTTPRequestHandler]:
    class StatusHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            status_code, content_type, body = load_status_payload(results_path)
            encoded = body.encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

            client_ip = self.client_address[0] if self.client_address else "unknown"
            log_line = (
                f"{utc_now_iso()} client={client_ip} path={self.path} "
                f"status={status_code} bytes={len(encoded)}\n"
            )
            try:
                access_log_path.parent.mkdir(parents=True, exist_ok=True)
                with access_log_path.open("a", encoding="utf-8") as fh:
                    fh.write(log_line)
            except OSError as exc:
                print(f"Warning: failed writing status access log: {exc}", file=sys.stderr)

        def log_message(self, fmt: str, *args: object) -> None:
            # Suppress default stderr HTTP chatter; we write structured request logs above.
            _ = (fmt, args)

    return StatusHandler


def parse_args(argv: list[str]) -> tuple[str, int, Path, Path]:
    base_dir = Path(__file__).resolve().parent
    host = "127.0.0.1"
    port = 8888
    results_path = base_dir / "data" / "results.json"
    access_log_path = base_dir / "logs" / "status_server.log"

    if len(argv) >= 1 and argv[0].strip():
        try:
            port = int(argv[0].strip())
        except ValueError as exc:
            raise ValueError(f"Port must be an integer (got '{argv[0]}')") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"Port must be between 1 and 65535 (got {port})")

    if len(argv) >= 2 and argv[1].strip():
        host = argv[1].strip()

    if len(argv) >= 3 and argv[2].strip():
        results_path = Path(argv[2].strip())

    if len(argv) >= 4 and argv[3].strip():
        access_log_path = Path(argv[3].strip())

    return host, port, results_path, access_log_path


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    server: ThreadingHTTPServer | None = None
    try:
        host, port, results_path, access_log_path = parse_args(args)
        handler_cls = make_handler(results_path, access_log_path)
        server = ThreadingHTTPServer((host, port), handler_cls)
        print(f"NetWatch status server listening on http://{host}:{port} (Ctrl+C to stop)")
        server.serve_forever()
        return 0
    except KeyboardInterrupt:
        print("\nStatus server stopped by user.")
        return 0
    except PermissionError as exc:
        print(f"Error: permission denied binding server port: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"Error: unable to start status server: {exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 3
    finally:
        if server is not None:
            server.server_close()


if __name__ == "__main__":
    sys.exit(main())