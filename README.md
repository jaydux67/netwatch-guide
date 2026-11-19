# NetWatch Project — Team Handover & Finalized Code Summary

This document captures the complete repository structure, finalized code blocks, verification steps, and plain-language explanations from our session so every team member has a clear reference inside the project folder.

---

## 1. Repository Folder Layout (`REPO_ROOT`)

```text
npd-26-1-gNN/
├── config/
│   ├── inventory.csv            # Primary 8-target inventory
│   └── inventory_16.csv         # 16-target workload for benchmarking
├── shell/
│   ├── netwatch.sh              # Part A entry point & Part B orchestrator
│   ├── run_scheduled.sh         # Wrapper script for unattended scheduled execution
│   └── lib/
│       └── common.sh            # Shared Bash validation, probing, reporting & alerting helpers
├── python/
│   ├── netwatch.py              # Part B TCP prober, concurrency engine & state diff
│   ├── status_server.py         # HTTP status server reading results.json
│   ├── status_client.py         # Raw TCP socket client fetching latest status
│   ├── data/
│   │   └── results.json         # Latest Part B JSON state snapshot
│   ├── reports/
│   │   ├── latest_results.csv   # Latest Part B CSV consumed by Part A
│   │   └── latest_status.txt    # Latest status report saved by status_client.py
│   └── logs/
│       └── status_server.log    # HTTP status server access log
├── data/
│   └── history.csv              # Cumulative Part A check history
├── reports/
│   └── netwatch_report_*.csv    # Timestamped integrated CSV reports
├── logs/
│   ├── netwatch.log             # Main pipeline execution log
│   ├── alerts.log               # Threshold alert log
│   ├── cron_netwatch.log        # Unattended scheduled run log (INT-04)
│   └── benchmark_runs.log       # 18-run performance benchmark output
├── archive/
│   └── netwatch_archive_*.tar.gz
├── docs/
│   ├── project_handover_summary.md
│   ├── network_architecture.png
│   ├── program_architecture.png
│   ├── group_report.pdf
│   └── individual/
│       └── <IndexNo>_individual_report.pdf
└── crontab.txt                  # Cron schedule definition (INT-04)
```

---

## 2. How Everything Fits Together (Plain-Language Overview)

1. **Part A (`shell/netwatch.sh` + `shell/lib/common.sh`):** Acts as the main command-line coordinator. It validates CLI flags (`-i`, `-o`, `-t`, `-w`, `-r`, `-h`), checks each row of `config/inventory.csv` (skipping bad rows with line numbers without crashing), runs Part A checks, calls `python/netwatch.py`, combines exit codes (`0`, `1`, `2`, `3`), generates a single-pass summary report (`reports/*.csv`), logs threshold breaches (`logs/alerts.log`), and archives old files.
2. **Part B (`python/netwatch.py`):** Uses only the Python standard library. It validates and deduplicates `(ip_address, port)` pairs, probes each TCP endpoint with a timeout (closing sockets cleanly in `finally`), runs both sequential (`seq`) and bounded `ThreadPoolExecutor` concurrent (`conc`) modes, compares open ports against the previous run (`Newly open`, `Newly closed`, `Unchanged open`), writes `results.json` and `latest_results.csv`, and prints `STATUS=OK|WARN|CRIT`.
3. **Status Service (`python/status_server.py` & `python/status_client.py`):** `status_server.py` serves a human-readable status summary from `results.json` over HTTP and logs every request; `status_client.py` connects using a raw TCP `socket`, reads until EOF, prints the report, and saves `latest_status.txt`.
4. **Scheduling (`crontab.txt` & `shell/run_scheduled.sh`):** Runs the full pipeline unattended on a recurring schedule and appends output to `logs/cron_netwatch.log`.

---

## 3. Finalized Code Reference

### 3.1 `shell/netwatch.sh` (`main()` Integration Entry Point)

```bash
main() {
  local inv_path="${REPO_ROOT}/config/inventory.csv"
  local out_dir="${REPO_ROOT}/reports"
  local archive_dir="${REPO_ROOT}/archive"
  local history_file="${REPO_ROOT}/data/history.csv"
  local log_file="${REPO_ROOT}/logs/netwatch.log"
  local timeout_sec="2"
  local threshold="95"
  local report_only=0
  local opt

  while getopts ":i:o:t:w:rh" opt; do
    case "${opt}" in
      i) inv_path="${OPTARG}" ;;
      o) out_dir="${OPTARG}" ;;
      t) timeout_sec="${OPTARG}" ;;
      w) threshold="${OPTARG}" ;;
      r) report_only=1 ;;
      h)
        usage
        return 0
        ;;
      :)
        printf 'Error: missing argument for -%s\n' "${OPTARG}" >&2
        return 3
        ;;
      \?)
        printf 'Error: unknown option -%s\n' "${OPTARG}" >&2
        return 3
        ;;
    esac
  done
  shift $((OPTIND - 1))

  if ! is_positive_number "${timeout_sec}"; then
    printf 'Error: timeout must be a positive number (%s)\n' "${timeout_sec}" >&2
    return 3
  fi
  if ! is_valid_threshold "${threshold}"; then
    printf 'Error: threshold must be between 0 and 100 (%s)\n' "${threshold}" >&2
    return 3
  fi

  init_dirs "${REPO_ROOT}" "${out_dir}" "${archive_dir}"
  if [[ ! -w "${out_dir}" ]]; then
    log_err "${log_file}" "Output directory not writable: ${out_dir}"
    return 3
  fi

  TMP_DIR="$(mktemp -d)"
  local valid_targets="${TMP_DIR}/valid.csv"
  : > "${valid_targets}"

  log_info "${log_file}" "Run started (timeout=${timeout_sec}s, threshold=${threshold}%, report_only=${report_only})"
  process_inventory "${inv_path}" "${timeout_sec}" "${report_only}" "${history_file}" "${log_file}" "${valid_targets}"

  local py_cmd=""
  if command -v python3 >/dev/null 2>&1; then
    py_cmd="python3"
  elif command -v python >/dev/null 2>&1; then
    py_cmd="python"
  fi

  local part_b_rc=0
  if [[ "${report_only}" -eq 0 ]]; then
    if [[ -z "${py_cmd}" ]]; then
      log_err "${log_file}" "Python interpreter not found; cannot run Part B stage"
      return 3
    fi
    "${py_cmd}" "${REPO_ROOT}/python/netwatch.py" both "${inv_path}" "${timeout_sec}" 4 || part_b_rc=$?
    log_info "${log_file}" "Part B finished with exit_code=${part_b_rc}"
    if [[ "${part_b_rc}" -eq 3 ]]; then
      log_err "${log_file}" "Part B failed with runtime/config error (exit 3)"
      return 3
    fi
  fi

  local stamp report_csv report_txt alerts_log part_b_csv report_rc alert_rc final_rc
  stamp="$(date -u +"%Y%m%dT%H%M%SZ")"
  report_csv="${out_dir}/netwatch_report_${stamp}.csv"
  report_txt="${TMP_DIR}/report_table.txt"
  alerts_log="${REPO_ROOT}/logs/alerts.log"
  part_b_csv="${REPO_ROOT}/python/reports/latest_results.csv"
  [[ -f "${part_b_csv}" ]] || part_b_csv="/dev/null"

  report_rc=0
  generate_report_one_pass "${history_file}" "${part_b_csv}" "${report_csv}" "${report_txt}" "${threshold}" || report_rc=$?
  if [[ "${report_rc}" -eq 3 ]]; then
    log_err "${log_file}" "Report generation aborted due to missing/empty history"
    return 3
  fi

  alert_rc=0
  emit_alerts_and_archive "${history_file}" "${threshold}" "${alerts_log}" "${archive_dir}" "${out_dir}" "${REPO_ROOT}/logs" 7 || alert_rc=$?
  if [[ "${alert_rc}" -eq 3 ]]; then
    return 3
  fi

  final_rc="${report_rc}"
  if [[ "${alert_rc}" -gt "${final_rc}" ]]; then
    final_rc="${alert_rc}"
  fi
  if [[ "${part_b_rc}" -gt "${final_rc}" ]]; then
    final_rc="${part_b_rc}"
  fi

  log_info "${log_file}" "Run finished (report=${report_csv}, exit_code=${final_rc})"
  return "${final_rc}"
}

main "$@"
```

---

### 3.2 `shell/lib/common.sh` (`emit_alerts_and_archive()` — ShellCheck Clean)

```bash
emit_alerts_and_archive() {
  local history_file="$1"
  local threshold="$2"
  local alerts_log="$3"
  local archive_dir="$4"
  local reports_dir="$5"
  local logs_dir="$6"
  local max_age_days="${7:-7}"

  mkdir -p "$(dirname "${alerts_log}")" "${archive_dir}"
  if [[ ! -w "${archive_dir}" ]]; then
    log_err "${alerts_log}" "Archive directory not writable: ${archive_dir}"
    return 3
  fi

  local ts use_color=0
  ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  if [[ -t 1 ]]; then
    use_color=1
  fi

  local worst_code=0
  if [[ -s "${history_file}" ]]; then
    local host pct state code plain_line color_line
    while IFS=',' read -r host _ _ pct; do
      state="NORMAL"
      code=0
      if awk -v p="${pct}" -v t="${threshold}" 'BEGIN { exit (p < t) ? 0 : 1 }'; then
        if awk -v p="${pct}" 'BEGIN { exit (p < 50.0) ? 0 : 1 }'; then
          state="CRITICAL"
          code=2
        else
          state="WARNING"
          code=1
        fi
      fi
      if [[ "${code}" -gt "${worst_code}" ]]; then
        worst_code="${code}"
      fi

      plain_line="${ts} [${state}] host=${host} availability=${pct}% (threshold=${threshold}%)"
      printf '%s\n' "${plain_line}" >> "${alerts_log}"

      if [[ "${use_color}" -eq 1 ]]; then
        case "${state}" in
          CRITICAL) color_line="\033[1;31m${plain_line}\033[0m" ;;
          WARNING)  color_line="\033[1;33m${plain_line}\033[0m" ;;
          *)        color_line="\033[0;32m${plain_line}\033[0m" ;;
        esac
        printf '%b\n' "${color_line}"
      else
        printf '%s\n' "${plain_line}"
      fi
    done < <(
      awk -F',' '
        NR > 1 && NF >= 8 {
          h = $2; st = $7
          gsub(/^[[:space:]]+|[[:space:]]+$/, "", h)
          gsub(/^[[:space:]]+|[[:space:]]+$/, "", st)
          tot[h]++
          if (st == "UP") up[h]++
        }
        END {
          for (h in tot) {
            u = (h in up) ? up[h] : 0
            pct = (tot[h] > 0) ? (u * 100.0 / tot[h]) : 0.0
            printf "%s,%d,%d,%.1f\n", h, u, tot[h], pct
          }
        }
      ' "${history_file}"
    )
  fi

  if [[ -n "${archive_dir}" && -d "${reports_dir}" && -d "${logs_dir}" ]]; then
    local stamp archive_file
    stamp="$(date -u +"%Y%m%dT%H%M%SZ")"
    archive_file="${archive_dir}/netwatch_archive_${stamp}.tar.gz"

    mapfile -t old_files < <(
      find "${reports_dir}" "${logs_dir}" -maxdepth 1 -type f -mtime "+${max_age_days}" 2>/dev/null || true
    )

    if [[ "${#old_files[@]}" -gt 0 ]]; then
      tar -czf "${archive_file}" -- "${old_files[@]}"
      local f
      for f in "${old_files[@]}"; do
        if [[ -n "${f}" && -f "${f}" ]]; then
          rm -f -- "${f}"
        fi
      done
      log_info "${alerts_log}" "Archived ${#old_files[@]} old files into ${archive_file}"
    else
      log_info "${alerts_log}" "Archive check complete: no files older than ${max_age_days} days"
    fi
  fi

  return "${worst_code}"
}
```

---

### 3.3 `python/netwatch.py`

```python
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
```

---

### 3.4 `python/status_server.py`

```python
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
```

---

### 3.5 `python/status_client.py`

```python
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
```

---

### 3.6 `crontab.txt` & `shell/run_scheduled.sh` (`INT-04`)

```cron
# crontab.txt
*/5 * * * * cd /e/npd-26-1-gNN && /usr/bin/env bash shell/netwatch.sh -i config/inventory.csv -o reports -t 2 -w 95 >> logs/cron_netwatch.log 2>&1
```

```bash
#!/usr/bin/env bash
# shell/run_scheduled.sh
set -euo pipefail

REPO_DIR="/e/npd-26-1-gNN"
cd "${REPO_DIR}"
mkdir -p logs
bash shell/netwatch.sh -i config/inventory.csv -o reports -t 1 -w 95 >> logs/cron_netwatch.log 2>&1
```

---

### 3.7 `config/inventory_16.csv` (16-Target Benchmark Workload)

```csv
hostname,ip_address,service,port,critical
intranet,127.0.0.1,http,8080,yes
warehouse-app,127.0.0.1,wms,9000,yes
fileserver,127.0.0.1,http,8081,no
closed-port-box,127.0.0.1,http,59999,no
lan-node-a,192.168.8.131,http,8082,yes
lan-node-b,192.168.8.131,api,8083,no
lab-target,192.168.8.131,http,8080,no
blackhole-gw,192.0.2.1,http,8080,no
app-node-1,127.0.0.1,http,8084,no
app-node-2,127.0.0.1,api,8085,no
db-cache-1,127.0.0.1,redis,16379,yes
queue-worker-1,127.0.0.1,wms,9001,no
edge-proxy-1,127.0.0.1,http,8086,no
storage-node-1,192.168.8.131,http,8087,no
telemetry-1,192.168.8.131,api,8088,no
sinkhole-2,192.0.2.2,http,8089,no
```

---

## 4. Benchmark Summary (3 Repeats per Cell, `timeout=1s`)

| Workload | Workers | Repeat 1 (`seq` / `conc`) | Repeat 2 (`seq` / `conc`) | Repeat 3 (`seq` / `conc`) | Mean `seq_sec` | Mean `conc_sec` | Speedup |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **8 targets** (`open=3, closed=5`) | `1` | `5.0714s` / `5.0707s` | `5.0567s` / `5.0702s` | `5.0643s` / `5.0554s` | **5.0641s** | **5.0621s** | **1.00x** |
| **8 targets** (`open=3, closed=5`) | `4` | `5.0821s` / `2.0152s` | `5.0740s` / `2.0198s` | `5.1147s` / `2.0317s` | **5.0903s** | **2.0222s** | **2.52x** |
| **8 targets** (`open=3, closed=5`) | `8` | `5.0512s` / `1.0156s` | `5.0713s` / `1.0144s` | `5.0686s` / `1.0133s` | **5.0637s** | **1.0144s** | **4.99x** |
| **16 targets** (`open=3, closed=13`) | `1` | `13.1411s` / `13.1578s` | `13.1352s` / `13.1319s` | `13.1118s` / `13.1438s` | **13.1294s** | **13.1445s** | **1.00x** |
| **16 targets** (`open=3, closed=13`) | `4` | `13.1237s` / `4.0227s` | `13.1827s` / `4.0387s` | `13.1551s` / `4.0323s` | **13.1538s** | **4.0312s** | **3.26x** |
| **16 targets** (`open=3, closed=13`) | `8` | `13.0980s` / `2.0116s` | `13.1253s` / `2.0094s` | `13.1023s` / `2.0121s` | **13.1085s** | **2.0110s** | **6.52x** |
