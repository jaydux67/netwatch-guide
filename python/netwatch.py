#!/usr/bin/env python3
"""NetWatch Part B: TCP socket checker, state comparison, and summary reporter."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import socket
import sys
import time
from typing import Any


EXIT_OK = 0
EXIT_WARN = 1
EXIT_CRIT = 2
EXIT_ERR = 3

VALID_MODES = ("seq", "conc", "both")
INVENTORY_HEADER = "hostname,ip_address,service,port,critical"


@dataclass(frozen=True)
class Target:
    hostname: str
    ip_address: str
    service: str
    port: int
    critical: str


@dataclass
class CheckResult:
    timestamp: str
    hostname: str
    ip_address: str
    service: str
    port: int
    critical: str
    state: str
    response_time_ms: float | None
    error: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_valid_ipv4(value: str) -> bool:
    try:
        parsed = ipaddress.IPv4Address(value)
        return str(parsed) == value
    except ValueError:
        return False


def is_valid_port_str(value: str) -> bool:
    if not value or not value.isdigit():
        return False
    if len(value) > 1 and value.startswith("0"):
        return False
    port = int(value)
    return 1 <= port <= 65535


def load_inventory(path: Path) -> tuple[list[Target], int, int]:
    """Load and validate inventory rows, rejecting bad rows and deduplicating (ip, port)."""
    if not path.exists():
        raise FileNotFoundError(f"Inventory file not found: {path}")
    if not path.is_file():
        raise OSError(f"Inventory path is not a file: {path}")

    try:
        raw_text = path.read_text(encoding="utf-8")
    except PermissionError as exc:
        raise PermissionError(f"Permission denied reading inventory: {path}") from exc
    except OSError as exc:
        raise OSError(f"Unable to read inventory: {path}") from exc

    if not raw_text.strip():
        raise ValueError(f"Inventory file is empty: {path}")

    accepted: list[Target] = []
    seen_pairs: set[tuple[str, int]] = set()
    accepted_count = 0
    rejected_count = 0
    header_checked = False

    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            rejected_count += 1
            print(f"Warning: line {line_no} rejected (blank line)", file=sys.stderr)
            continue

        if not header_checked:
            header_checked = True
            if line == INVENTORY_HEADER:
                continue

        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 5:
            rejected_count += 1
            print(
                f"Warning: line {line_no} rejected (expected 5 fields, got {len(parts)})",
                file=sys.stderr,
            )
            continue

        hostname, ip_str, service, port_str, critical = parts
        if not hostname:
            rejected_count += 1
            print(f"Warning: line {line_no} rejected (empty hostname)", file=sys.stderr)
            continue
        if not is_valid_ipv4(ip_str):
            rejected_count += 1
            print(f"Warning: line {line_no} rejected (invalid ip_address '{ip_str}')", file=sys.stderr)
            continue
        if not service:
            rejected_count += 1
            print(f"Warning: line {line_no} rejected (empty service)", file=sys.stderr)
            continue
        if not is_valid_port_str(port_str):
            rejected_count += 1
            print(f"Warning: line {line_no} rejected (invalid port '{port_str}')", file=sys.stderr)
            continue
        if critical not in ("yes", "no"):
            rejected_count += 1
            print(
                f"Warning: line {line_no} rejected (invalid critical flag '{critical}')",
                file=sys.stderr,
            )
            continue

        port_num = int(port_str)
        pair = (ip_str, port_num)
        if pair in seen_pairs:
            rejected_count += 1
            print(
                f"Warning: line {line_no} skipped (duplicate address/port {ip_str}:{port_num})",
                file=sys.stderr,
            )
            continue

        seen_pairs.add(pair)
        accepted.append(Target(hostname, ip_str, service, port_num, critical))
        accepted_count += 1

    return accepted, accepted_count, rejected_count


def probe_tcp(target: Target, timeout_sec: float) -> CheckResult:
    """Probe one TCP target with a timeout; return status value and close socket cleanly."""
    ts = utc_now_iso()
    sock: socket.socket | None = None
    start = time.perf_counter()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout_sec)
        sock.connect((target.ip_address, target.port))
        elapsed_ms = round((time.perf_counter() - start) * 1000.0, 2)
        return CheckResult(
            timestamp=ts,
            hostname=target.hostname,
            ip_address=target.ip_address,
            service=target.service,
            port=target.port,
            critical=target.critical,
            state="OPEN",
            response_time_ms=elapsed_ms,
            error="",
        )
    except socket.timeout:
        elapsed_ms = round((time.perf_counter() - start) * 1000.0, 2)
        return CheckResult(
            ts,
            target.hostname,
            target.ip_address,
            target.service,
            target.port,
            target.critical,
            "CLOSED",
            elapsed_ms,
            "timeout",
        )
    except socket.gaierror as exc:
        elapsed_ms = round((time.perf_counter() - start) * 1000.0, 2)
        return CheckResult(
            ts,
            target.hostname,
            target.ip_address,
            target.service,
            target.port,
            target.critical,
            "CLOSED",
            elapsed_ms,
            f"name_resolution_error:{exc}",
        )
    except OSError as exc:
        elapsed_ms = round((time.perf_counter() - start) * 1000.0, 2)
        detail = exc.strerror if exc.strerror else str(exc)
        return CheckResult(
            ts,
            target.hostname,
            target.ip_address,
            target.service,
            target.port,
            target.critical,
            "CLOSED",
            elapsed_ms,
            detail,
        )
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def run_sequential(targets: list[Target], timeout_sec: float) -> tuple[list[CheckResult], float]:
    start = time.perf_counter()
    results = [probe_tcp(target, timeout_sec) for target in targets]
    duration = round(time.perf_counter() - start, 4)
    return results, duration


def run_concurrent(
    targets: list[Target], timeout_sec: float, max_workers: int
) -> tuple[list[CheckResult], float]:
    if not targets:
        return [], 0.0

    bounded_workers = max(1, min(max_workers, len(targets)))
    start = time.perf_counter()
    collected: dict[int, CheckResult] = {}

    with ThreadPoolExecutor(max_workers=bounded_workers) as executor:
        futures = {
            executor.submit(probe_tcp, target, timeout_sec): idx
            for idx, target in enumerate(targets)
        }
        for future in as_completed(futures):
            idx = futures[future]
            target = targets[idx]
            try:
                collected[idx] = future.result()
            except (OSError, RuntimeError, ValueError) as exc:
                collected[idx] = CheckResult(
                    timestamp=utc_now_iso(),
                    hostname=target.hostname,
                    ip_address=target.ip_address,
                    service=target.service,
                    port=target.port,
                    critical=target.critical,
                    state="CLOSED",
                    response_time_ms=None,
                    error=f"worker_error:{exc}",
                )

    ordered = [collected[idx] for idx in range(len(targets))]
    duration = round(time.perf_counter() - start, 4)
    return ordered, duration


def diff_states(
    current: list[CheckResult], previous_json: Path
) -> tuple[bool, list[str], list[str], list[str]]:
    """Compare current OPEN (ip, port) set with previous run using set operations."""
    curr_open: set[tuple[str, int]] = {
        (item.ip_address, item.port) for item in current if item.state == "OPEN"
    }

    if not previous_json.exists():
        return False, [], [], sorted(f"{ip}:{port}" for ip, port in curr_open)

    try:
        raw_content = previous_json.read_text(encoding="utf-8")
        data = json.loads(raw_content)
        records = data.get("results", [])
        if not isinstance(records, list):
            raise ValueError("Malformed results list in previous JSON")
        prev_open: set[tuple[str, int]] = {
            (str(rec["ip_address"]), int(rec["port"]))
            for rec in records
            if isinstance(rec, dict) and rec.get("state") == "OPEN"
        }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(
            f"Warning: previous results unreadable ({exc}); starting fresh baseline",
            file=sys.stderr,
        )
        return False, [], [], sorted(f"{ip}:{port}" for ip, port in curr_open)

    newly_open = sorted(f"{ip}:{port}" for ip, port in (curr_open - prev_open))
    newly_closed = sorted(f"{ip}:{port}" for ip, port in (prev_open - curr_open))
    unchanged = sorted(f"{ip}:{port}" for ip, port in (curr_open & prev_open))
    return True, newly_open, newly_closed, unchanged


def write_outputs(
    results: list[CheckResult],
    seq_duration: float,
    conc_duration: float,
    worker_limit: int,
    json_path: Path,
    csv_path: Path,
) -> None:
    try:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.parent.mkdir(parents=True, exist_ok=True)

        payload: dict[str, Any] = {
            "generated_at": utc_now_iso(),
            "worker_limit": worker_limit,
            "sequential_duration_sec": seq_duration,
            "concurrent_duration_sec": conc_duration,
            "results": [asdict(item) for item in results],
        }
        json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        csv_lines = ["timestamp,hostname,ip_address,service,port,critical,state,rtt_ms,error"]
        for item in results:
            rtt_val = "" if item.response_time_ms is None else str(item.response_time_ms)
            safe_error = item.error.replace(",", ";")
            csv_lines.append(
                f"{item.timestamp},{item.hostname},{item.ip_address},{item.service},"
                f"{item.port},{item.critical},{item.state},{rtt_val},{safe_error}"
            )
        csv_path.write_text("\n".join(csv_lines) + "\n", encoding="utf-8")
    except PermissionError as exc:
        raise PermissionError(f"Permission denied writing output files: {exc}") from exc
    except OSError as exc:
        raise OSError(f"Failed writing output files: {exc}") from exc


def summarize_and_exit_code(
    results: list[CheckResult], seq_duration: float, conc_duration: float
) -> tuple[str, int]:
    total = len(results)
    open_count = sum(1 for item in results if item.state == "OPEN")
    closed_count = total - open_count

    grouped_by_host: dict[str, list[CheckResult]] = {}
    for item in results:
        grouped_by_host.setdefault(item.hostname, []).append(item)

    host_rows: list[str] = []
    critical_down = False
    for host in sorted(grouped_by_host.keys()):
        items = grouped_by_host[host]
        up_items = sum(1 for item in items if item.state == "OPEN")
        avail_pct = (up_items / len(items) * 100.0) if items else 0.0
        host_rows.append(f"  {host:<18} {up_items}/{len(items)} ({avail_pct:5.1f}%)")
        if any(item.critical == "yes" and item.state != "OPEN" for item in items):
            critical_down = True

    slowest_items = sorted(
        (item for item in results if item.response_time_ms is not None),
        key=lambda item: (-item.response_time_ms, item.hostname, item.port, item.service),
    )[:3]

    if total == 0:
        status_label = "OK"
        exit_code = EXIT_OK
    elif critical_down or open_count == 0:
        status_label = "CRIT"
        exit_code = EXIT_CRIT
    elif closed_count > 0:
        status_label = "WARN"
        exit_code = EXIT_WARN
    else:
        status_label = "OK"
        exit_code = EXIT_OK

    lines = [
        f"Summary: total={total} open={open_count} closed={closed_count} "
        f"seq_sec={seq_duration} conc_sec={conc_duration}",
        "Availability per host:",
        *(host_rows if host_rows else ["  (no targets)"]),
        "Slowest services:",
        *(
            [
                f"  {item.hostname}:{item.port} ({item.service}) "
                f"{item.response_time_ms} ms [{item.state}]"
                for item in slowest_items
            ]
            if slowest_items
            else ["  (none)"]
        ),
        f"STATUS={status_label}",
    ]
    return "\n".join(lines), exit_code


def parse_cli_args(argv: list[str]) -> tuple[str, Path, float, int]:
    if len(argv) != 4:
        raise ValueError(
            "Usage: python/netwatch.py <mode: seq|conc|both> <inventory_path> <timeout_sec> <worker_limit>"
        )

    mode = argv[0].strip()
    if mode not in VALID_MODES:
        raise ValueError(f"Unknown mode '{mode}' (must be one of: seq, conc, both)")

    inventory_path = Path(argv[1])

    try:
        timeout_sec = float(argv[2])
    except ValueError as exc:
        raise ValueError(f"Invalid timeout '{argv[2]}' (must be a number)") from exc
    if timeout_sec <= 0:
        raise ValueError(f"Timeout must be greater than 0 (got {timeout_sec})")

    raw_workers = argv[3].strip()
    try:
        worker_limit = int(raw_workers)
    except ValueError as exc:
        raise ValueError(f"Invalid worker limit '{argv[3]}' (must be a positive integer)") from exc
    if worker_limit <= 0:
        raise ValueError(f"Worker limit must be greater than 0 (got {worker_limit})")

    return mode, inventory_path, timeout_sec, worker_limit


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        mode, inventory_path, timeout_sec, worker_limit = parse_cli_args(args)
        targets, accepted_count, rejected_count = load_inventory(inventory_path)

        seq_results: list[CheckResult] = []
        conc_results: list[CheckResult] = []
        seq_sec = 0.0
        conc_sec = 0.0

        if mode in ("seq", "both"):
            seq_results, seq_sec = run_sequential(targets, timeout_sec)
        if mode in ("conc", "both"):
            conc_results, conc_sec = run_concurrent(targets, timeout_sec, worker_limit)

        final_results = conc_results if mode == "conc" else (seq_results if mode == "seq" else conc_results)

        base_dir = Path(__file__).resolve().parent
        json_out = base_dir / "data" / "results.json"
        csv_out = base_dir / "reports" / "latest_results.csv"

        has_previous, newly_open, newly_closed, unchanged = diff_states(final_results, json_out)
        write_outputs(final_results, seq_sec, conc_sec, worker_limit, json_out, csv_out)

        summary_text, exit_code = summarize_and_exit_code(final_results, seq_sec, conc_sec)
        print(f"Inventory: accepted={accepted_count} rejected={rejected_count}")
        if not has_previous:
            print("State diff: no previous run found; baseline recorded.")
        else:
            print(f"Newly open: {', '.join(newly_open) if newly_open else 'none'}")
            print(f"Newly closed: {', '.join(newly_closed) if newly_closed else 'none'}")
            print(f"Unchanged open: {', '.join(unchanged) if unchanged else 'none'}")
        print(summary_text)
        return exit_code

    except KeyboardInterrupt:
        print("Interrupted by user.", file=sys.stderr)
        return EXIT_ERR
    except (ValueError, FileNotFoundError, PermissionError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_ERR


if __name__ == "__main__":
    sys.exit(main())