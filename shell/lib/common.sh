#!/usr/bin/env bash
# Shared helper library for NetWatch (sourced by shell/netwatch.sh).

init_dirs() {
  local base_dir="$1"
  local out_dir="$2"
  local archive_dir="$3"
  mkdir -p "${base_dir}/data" "${base_dir}/logs" "${base_dir}/reports" \
           "${out_dir}" "${archive_dir}"
}

log_info() {
  local log_file="$1"
  local msg="$2"
  local ts
  ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  printf '%s [INFO] %s\n' "${ts}" "${msg}" >> "${log_file}"
}

log_warn() {
  local log_file="$1"
  local msg="$2"
  local ts
  ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  printf '%s [WARN] %s\n' "${ts}" "${msg}" >> "${log_file}"
}

log_err() {
  local log_file="$1"
  local msg="$2"
  local ts
  ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  printf '%s [ERR]  %s\n' "${ts}" "${msg}" >&2
  if [[ -n "${log_file:-}" && -w "$(dirname "${log_file}")" ]]; then
    printf '%s [ERR]  %s\n' "${ts}" "${msg}" >> "${log_file}"
  fi
}

append_history() {
  local history_file="$1"
  local ts="$2"
  local hostname="$3"
  local ip="$4"
  local service="$5"
  local port="$6"
  local critical="$7"
  local status="$8"
  local rtt_ms="$9"

  if [[ ! -f "${history_file}" ]]; then
    printf 'timestamp,hostname,ip_address,service,port,critical,status,rtt_ms\n' > "${history_file}"
  fi
  printf '%s,%s,%s,%s,%s,%s,%s,%s\n' \
    "${ts}" "${hostname}" "${ip}" "${service}" "${port}" "${critical}" "${status}" "${rtt_ms}" \
    >> "${history_file}"
}

is_valid_ipv4() {
  local ip="$1"
  local oct1 oct2 oct3 oct4

  [[ "${ip}" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  oct1="${BASH_REMATCH[1]}"
  oct2="${BASH_REMATCH[2]}"
  oct3="${BASH_REMATCH[3]}"
  oct4="${BASH_REMATCH[4]}"

  local o
  for o in "${oct1}" "${oct2}" "${oct3}" "${oct4}"; do
    if [[ "${#o}" -gt 1 && "${o:0:1}" == "0" ]]; then
      return 1
    fi
    (( o >= 0 && o <= 255 )) || return 1
  done
  return 0
}

is_valid_port() {
  local port="$1"
  [[ "${port}" =~ ^[0-9]+$ ]] || return 1
  if [[ "${#port}" -gt 1 && "${port:0:1}" == "0" ]]; then
    return 1
  fi
  (( port >= 1 && port <= 65535 )) || return 1
  return 0
}

is_positive_number() {
  local val="$1"
  [[ "${val}" =~ ^([0-9]+([.][0-9]+)?|[.][0-9]+)$ ]] || return 1
  awk -v n="${val}" 'BEGIN { exit (n > 0) ? 0 : 1 }'
}

is_valid_threshold() {
  local thr="$1"
  [[ "${thr}" =~ ^[0-9]+([.][0-9]+)?$ ]] || return 1
  awk -v n="${thr}" 'BEGIN { exit (n >= 0 && n <= 100) ? 0 : 1 }'
}

generate_report_one_pass() {
  local history_file="$1"
  local part_b_csv="$2"
  local report_csv="$3"
  local report_txt="$4"
  local threshold="$5"

  if [[ ! -s "${history_file}" ]]; then
    log_err "" "Report skipped: history file missing or empty (${history_file})"
    return 3
  fi

  awk -F',' -v thr="${threshold}" -v out_csv="${report_csv}" '
    BEGIN {
      OFS = ","
      have_b = 0
    }
    NR == FNR {
      # Skip header of history.csv
      if (FNR == 1 && $1 == "timestamp") next
      if (NF < 8) next
      ts = $1; host = $2; ip = $3; svc = $4; port = $5; crit = $6; status = $7; rtt = $8
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", host)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", svc)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", status)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", rtt)

      total_checks++
      status_cnt[status]++
      host_total[host]++
      if (status == "UP") {
        host_up[host]++
      } else {
        svc_fail[svc]++
      }

      # Extract hour YYYY-MM-DDTHH from ISO timestamp
      hr = substr(ts, 1, 13)
      if (length(hr) == 13) {
        hour_cnt[hr]++
      }
      next
    }
    {
      # Second pass: Part B CSV (if present)
      if (FNR == 1 && $1 == "timestamp") next
      if (NF >= 7) {
        have_b = 1
        b_host = $2; b_svc = $4; b_port = $5; b_state = $7
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", b_host)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", b_svc)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", b_state)
        b_total++
        b_state_cnt[b_state]++
      }
    }
    END {
      if (total_checks == 0) {
        exit 3
      }

      print "section,key,metric1,metric2,metric3" > out_csv
      print "overall,total_checks," total_checks ",," > out_csv
      for (st in status_cnt) {
        print "status," st "," status_cnt[st] ",," > out_csv
      }

      worst_code = 0
      print "=== HOST AVAILABILITY ==="
      printf "%-18s %-8s %-8s %-10s %-10s\n", "HOST", "UP", "TOTAL", "AVAIL_%", "STATE"
      for (h in host_total) {
        up_n = (h in host_up) ? host_up[h] : 0
        tot_n = host_total[h]
        pct = (tot_n > 0) ? (up_n * 100.0 / tot_n) : 0.0
        state = "NORMAL"
        code = 0
        if (pct < thr) {
          if (pct < 50.0) {
            state = "CRITICAL"
            code = 2
          } else {
            state = "WARNING"
            code = 1
          }
        }
        if (code > worst_code) worst_code = code
        printf "%-18s %-8d %-8d %9.1f%% %-10s\n", h, up_n, tot_n, pct, state
        print "host," h "," up_n "," tot_n "," sprintf("%.1f", pct) > out_csv
      }

      print "\n=== TOP 3 FAILING SERVICES ==="
      # Selection sort top 3 from svc_fail
      for (k = 1; k <= 3; k++) {
        best_svc = ""
        best_val = -1
        for (s in svc_fail) {
          val = svc_fail[s]
          if (val > best_val || (val == best_val && s < best_svc)) {
            best_val = val
            best_svc = s
          }
        }
        if (best_val <= 0 || best_svc == "") break
        printf "  %d. %-12s failures=%d\n", k, best_svc, best_val
        print "top_failing_service," best_svc "," best_val ",," > out_csv
        delete svc_fail[best_svc]
      }

      print "\n=== CHECKS PER HOUR ==="
      for (hr in hour_cnt) {
        printf "  %-15s checks=%d\n", hr, hour_cnt[hr]
        print "checks_per_hour," hr "," hour_cnt[hr] ",," > out_csv
      }

      print "\n=== SUMMARY COUNTS ==="
      printf "Total checks: %d | UP: %d | DOWN: %d\n",
        total_checks,
        ("UP" in status_cnt ? status_cnt["UP"] : 0),
        ("DOWN" in status_cnt ? status_cnt["DOWN"] : 0)

      if (have_b == 1) {
        printf "Part B records: %d (OPEN=%d, CLOSED=%d)\n",
          b_total,
          ("OPEN" in b_state_cnt ? b_state_cnt["OPEN"] : 0),
          ("CLOSED" in b_state_cnt ? b_state_cnt["CLOSED"] : 0)
        print "part_b,records," b_total ",," > out_csv
      } else {
        print "Note: Part B results CSV not yet present; history report generated."
      }
      exit worst_code
    }
  ' "${history_file}" "${part_b_csv}" | sed 's/[[:space:]]*$//' | grep -v '^$' > "${report_txt}"

  local rc=$?
  if [[ "${rc}" -eq 3 ]]; then
    log_err "" "Report generation failed: no completed checks in ${history_file}"
    return 3
  fi
  cat "${report_txt}"
  return "${rc}"
}
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
