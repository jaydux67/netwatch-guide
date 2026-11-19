#!/usr/bin/env bash
# Name:        netwatch.sh
# Purpose:     Part A Bash script for NetWatch option parsing, inventory
#              validation, bounded host probing, and logging.
# Authors:     NetWatch Team (Member 1)
# Usage:       ./shell/netwatch.sh [-i inventory] [-o out_dir] [-t timeout]
#                                  [-w threshold] [-r] [-h]

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
# shellcheck source=shell/lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

TMP_DIR=""
cleanup() {
  local exit_code=$?
  if [[ -n "${TMP_DIR}" && -d "${TMP_DIR}" ]]; then
    rm -rf -- "${TMP_DIR}"
  fi
  exit "${exit_code}"
}
trap cleanup EXIT INT TERM

usage() {
  cat <<'EOF'
Usage: netwatch.sh [options]

Options:
  -i PATH   Inventory CSV path (default: config/inventory.csv)
  -o DIR    Output directory (default: reports)
  -t SEC    Probe timeout in seconds > 0 (default: 2)
  -w PCT    Availability threshold 0..100 (default: 95)
  -r        Report-only mode
  -h        Print usage and exit 0
EOF
}

probe_host() {
  local host="$1"
  local timeout_sec="$2"
  local ping_rc=0
  local output=""
  local rtt=""

  if [[ "$(uname -s)" == "Darwin" ]]; then
    local ms
    ms="$(awk -v t="${timeout_sec}" 'BEGIN { v = int(t * 1000); print (v < 100) ? 100 : v }')"
    output="$(ping -n -c 1 -W "${ms}" "${host}" 2>/dev/null)" || ping_rc=$?
  else
    local sec
    sec="$(awk -v t="${timeout_sec}" 'BEGIN { v = int(t + 0.999); print (v < 1) ? 1 : v }')"
    output="$(ping -n -c 1 -W "${sec}" "${host}" 2>/dev/null)" || ping_rc=$?
  fi

  if [[ "${ping_rc}" -eq 0 ]]; then
    rtt="$(printf '%s\n' "${output}" | sed -n 's/.*time[=<]\([0-9.][0-9.]*\).*/\1/p' | head -n 1)"
    printf 'UP|%s\n' "${rtt:-N/A}"
    return 0
  fi

  printf 'DOWN|N/A\n'
  return 0
}

process_inventory() {
  local inv_file="$1"
  local timeout_sec="$2"
  local report_only="$3"
  local history_file="$4"
  local log_file="$5"
  local valid_out="$6"

  if [[ ! -e "${inv_file}" ]]; then
    log_err "${log_file}" "Inventory file missing: ${inv_file}"
    return 3
  fi
  if [[ ! -r "${inv_file}" ]]; then
    log_err "${log_file}" "Inventory file not readable: ${inv_file}"
    return 3
  fi
  if [[ ! -s "${inv_file}" ]]; then
    log_err "${log_file}" "Inventory file empty: ${inv_file}"
    return 3
  fi

  local line_no=0
  local seen_header=0
  local line clean field_count
  local hostname ip service port critical

  while IFS= read -r line || [[ -n "${line}" ]]; do
    line_no=$((line_no + 1))
    clean="${line%$'\r'}"
    clean="${clean#"${clean%%[![:space:]]*}"}"
    clean="${clean%"${clean##*[![:space:]]}"}"

    if [[ -z "${clean}" ]]; then
      log_err "${log_file}" "Line ${line_no} rejected: blank line"
      continue
    fi

    if [[ "${seen_header}" -eq 0 ]]; then
      seen_header=1
      if [[ "${clean}" == "hostname,ip_address,service,port,critical" ]]; then
        continue
      fi
    fi

    local commas="${clean//[^,]}"
    field_count=$(( ${#commas} + 1 ))
    if [[ "${field_count}" -ne 5 ]]; then
      log_err "${log_file}" "Line ${line_no} rejected: expected 5 fields, got ${field_count}"
      continue
    fi

    IFS=',' read -r hostname ip service port critical <<< "${clean}"
    hostname="${hostname#"${hostname%%[![:space:]]*}"}"; hostname="${hostname%"${hostname##*[![:space:]]}"}"
    ip="${ip#"${ip%%[![:space:]]*}"}"; ip="${ip%"${ip##*[![:space:]]}"}"
    service="${service#"${service%%[![:space:]]*}"}"; service="${service%"${service##*[![:space:]]}"}"
    port="${port#"${port%%[![:space:]]*}"}"; port="${port%"${port##*[![:space:]]}"}"
    critical="${critical#"${critical%%[![:space:]]*}"}"; critical="${critical%"${critical##*[![:space:]]}"}"

    if [[ -z "${hostname}" ]]; then
      log_err "${log_file}" "Line ${line_no} rejected: field 'hostname' empty"
      continue
    fi
    if ! is_valid_ipv4 "${ip}"; then
      log_err "${log_file}" "Line ${line_no} rejected: field 'ip_address' invalid ('${ip}')"
      continue
    fi
    if [[ -z "${service}" ]]; then
      log_err "${log_file}" "Line ${line_no} rejected: field 'service' empty"
      continue
    fi
    if ! is_valid_port "${port}"; then
      log_err "${log_file}" "Line ${line_no} rejected: field 'port' invalid ('${port}')"
      continue
    fi
    if [[ "${critical}" != "yes" && "${critical}" != "no" ]]; then
      log_err "${log_file}" "Line ${line_no} rejected: field 'critical' invalid ('${critical}')"
      continue
    fi

    printf '%s,%s,%s,%s,%s\n' "${hostname}" "${ip}" "${service}" "${port}" "${critical}" >> "${valid_out}"

    if [[ "${report_only}" -eq 0 ]]; then
      local ts probe_res status rtt
      ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
      probe_res="$(probe_host "${ip}" "${timeout_sec}")"
      status="${probe_res%%|*}"
      rtt="${probe_res#*|}"
      append_history "${history_file}" "${ts}" "${hostname}" "${ip}" "${service}" "${port}" "${critical}" "${status}" "${rtt}"
      log_info "${log_file}" "Probed ${hostname} (${ip}:${port} ${service}) -> ${status} rtt=${rtt}"
      printf '%-18s %-15s %-8s %-6s %-5s rtt=%s\n' "${hostname}" "${ip}" "${service}" "${port}" "${status}" "${rtt}"
    fi
  done < "${inv_file}"

  return 0
}
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