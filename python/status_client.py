#!/usr/bin/env python3
"""NetWatch Part B socket client: fetches status from status_server.py."""

from __future__ import annotations

from pathlib import Path
import socket
import sys


def fetch_status(host: str, port: int, timeout_sec: float) -> str:
    sock: socket.socket | None = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout_sec)
        sock.connect((host, port))

        request = (
            f"GET / HTTP/1.0\r\n"
            f"Host: {host}:{port}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8")
        sock.sendall(request)

        chunks: list[bytes] = []
        while True:
            data = sock.recv(1024)
            if not data:
                break
            chunks.append(data)

        raw_bytes = b"".join(chunks)
        text = raw_bytes.decode("utf-8", errors="replace")
        if "\r\n\r\n" in text:
            _, body = text.split("\r\n\r\n", 1)
            return body
        if "\n\n" in text:
            _, body = text.split("\n\n", 1)
            return body
        return text
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def parse_args(argv: list[str]) -> tuple[str, int, float, Path]:
    base_dir = Path(__file__).resolve().parent
    host = "127.0.0.1"
    port = 8888
    timeout_sec = 3.0
    out_file = base_dir / "reports" / "latest_status.txt"

    if len(argv) >= 1 and argv[0].strip():
        host = argv[0].strip()
    if len(argv) >= 2 and argv[1].strip():
        try:
            port = int(argv[1].strip())
        except ValueError as exc:
            raise ValueError(f"Port must be an integer (got '{argv[1]}')") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"Port must be between 1 and 65535 (got {port})")

    if len(argv) >= 3 and argv[2].strip():
        try:
            timeout_sec = float(argv[2].strip())
        except ValueError as exc:
            raise ValueError(f"Timeout must be numeric (got '{argv[2]}')") from exc
    if timeout_sec <= 0:
        raise ValueError(f"Timeout must be greater than 0 (got {timeout_sec})")

    if len(argv) >= 4 and argv[3].strip():
        out_file = Path(argv[3].strip())

    return host, port, timeout_sec, out_file


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        host, port, timeout_sec, out_file = parse_args(args)
        body = fetch_status(host, port, timeout_sec)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(body, encoding="utf-8")
        print(body, end="")
        return 0
    except KeyboardInterrupt:
        print("\nClient interrupted by user.", file=sys.stderr)
        return 3
    except socket.timeout:
        print("Error: timed out waiting for status server response.", file=sys.stderr)
        return 3
    except UnicodeDecodeError as exc:
        print(f"Error: failed decoding server response: {exc}", file=sys.stderr)
        return 3
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())