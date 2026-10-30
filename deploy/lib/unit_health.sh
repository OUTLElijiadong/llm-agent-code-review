#!/usr/bin/env bash
# 读取 systemd 定时任务（oneshot）最近一次执行结果，供 ops-check.sh 汇总。
# 此文件只提供函数，不应单独执行；由 ops-check.sh source，依赖 lib/common.sh 的
# latest_file_by_mtime，以及调用方提供的 json_escape。
#
# 背景：生产 prism-maintenance.service 从 2026-10-24 起连续 7 天失败、恢复验证 10-25 失败，
# 但 10-30 的日常巡检仍输出“全部生产关键检查通过”，因为巡检从不读这两个单元的状态。
#
# 判定（job_state）：
#   ok            最近一次执行成功
#   failed        最近一次执行失败（ActiveState=failed 或 Result 不是 success）
#   running       正在执行，不据此判失败
#   never_run     本次开机以来没有执行记录。systemd 重启后会丢失单元的历史执行信息，
#                 这不等于失败，也不能证明成功，所以只如实报告，不降级
#   not_installed 单元不存在（例如开发机），不适用
#   unknown       无法读取（没有 systemctl）
# 巡检状态（status）只有 ok / degraded：failed，或最近一次成功但已超过 max_age 小时都算 degraded。
# 它不会产生阻断（error）：发布流程会在发布后运行巡检，历史上的维护失败不能让发布回滚。

# 读取单元的一项 systemd 属性值。
# 参数: $1 单元名；$2 属性名。
# 返回: stdout 输出属性值（可能为空）；systemctl 不可用或失败时返回 1。
unit_property() {
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl show "$1" --property="$2" --value 2>/dev/null
}

# 把时间戳属性（如 "Sun 2026-10-25 03:18:29 CST"）转成 Unix 秒。
# 参数: $1 时间文本。
# 返回: stdout 输出秒数；无法解析返回 1。
parse_unit_timestamp() {
  local text="$1" epoch="" stripped
  [[ -n "$text" && "$text" != "n/a" ]] || return 1
  # 生产是 Linux GNU date，直接支持 `date -d`（已在生产验证 "Sun 2026-10-25 03:18:29 CST"）。
  epoch="$(date -d "$text" +%s 2>/dev/null)" || epoch=""
  if [[ ! "$epoch" =~ ^[0-9]+$ ]]; then
    # BSD date（macOS，仅本地测试用）没有 -d：去掉星期与时区缩写后按本地时间解析。
    stripped="$(printf '%s' "$text" | sed -E 's/^[A-Za-z]{3} //; s/ [A-Za-z]{2,5}$//')"
    epoch="$(date -j -f '%Y-%m-%d %H:%M:%S' "$stripped" +%s 2>/dev/null)" || epoch=""
  fi
  [[ "$epoch" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$epoch"
}

# 采集一个 oneshot 单元最近一次执行的状态，写入以 $1 为前缀的全局变量：
#   <前缀>_job_state / _status / _stale / _result / _exit_status / _exit_at / _age_hours / _reason
# 参数: $1 变量前缀；$2 单元名；$3 最大允许年龄（小时，仅对“最近一次成功”判定过期）；
#       $4 可选，单元自己的日志目录；$5 可选，日志文件名前缀。
# 返回: 始终返回 0。
collect_unit_health() {
  local prefix="$1" unit="$2" max_age="$3" log_dir="${4:-}" log_prefix="${5:-}"
  local job_state="ok" status="ok" stale=false result="" exit_status="" exit_at="" age=-1 reason=""
  local load_state active_state entered_at epoch="" latest_log="" tail_text=""

  if ! command -v systemctl >/dev/null 2>&1; then
    job_state=unknown
    reason="systemctl 不可用，无法读取最近一次执行结果"
  else
    load_state="$(unit_property "$unit" LoadState || true)"
    if [[ -z "$load_state" || "$load_state" == "not-found" ]]; then
      job_state=not_installed
      reason="单元未安装"
    else
      active_state="$(unit_property "$unit" ActiveState || true)"
      result="$(unit_property "$unit" Result || true)"
      exit_status="$(unit_property "$unit" ExecMainStatus || true)"
      exit_at="$(unit_property "$unit" ExecMainExitTimestamp || true)"
      entered_at="$(unit_property "$unit" StateChangeTimestamp || true)"
      epoch="$(parse_unit_timestamp "$exit_at" || parse_unit_timestamp "$entered_at" || true)"
      if [[ "$epoch" =~ ^[0-9]+$ ]]; then
        age=$(( ( $(date +%s) - epoch ) / 3600 ))
        (( age >= 0 )) || age=0
      fi
      if [[ "$active_state" == activating || "$active_state" == active || "$active_state" == reloading ]]; then
        job_state=running
        reason="正在执行"
        age=-1
      elif [[ "$active_state" == failed || ( -n "$result" && "$result" != success ) ]]; then
        job_state=failed
        status=degraded
        reason="result=${result:-unknown} exit=${exit_status:-?}"
        # 失败原因的来源：单元自己的日志目录（维护任务的错误原文只在那里，journal 只有
        # status=1/FAILURE）；没有日志目录时（如恢复验证）取 journal 里最近的 ERROR 行。
        if [[ -n "$log_dir" && -n "$log_prefix" && -d "$log_dir" ]]; then
          latest_log="$(latest_file_by_mtime "$log_dir" "${log_prefix}*.log" || true)"
          if [[ -n "$latest_log" && -r "$latest_log" ]]; then
            tail_text="$(tail -n 3 "$latest_log" 2>/dev/null | tr '\n' ' ' | cut -c1-240 || true)"
          fi
        elif command -v journalctl >/dev/null 2>&1; then
          tail_text="$(journalctl -u "$unit" -n 40 --no-pager -o cat 2>/dev/null \
            | grep -E 'ERROR|FAILURE|失败|fatal' | tail -n 2 | tr '\n' ' ' | cut -c1-240 || true)"
        fi
        [[ -z "$tail_text" ]] || reason="${reason}; 最近日志: ${tail_text}"
      elif [[ -z "$exit_at" || "$exit_at" == "n/a" ]]; then
        job_state=never_run
        reason="本次开机以来没有执行记录（systemd 重启后会丢失历史，不代表失败也不代表成功）"
      elif [[ "$max_age" =~ ^[0-9]+$ ]] && (( age > max_age )); then
        stale=true
        status=degraded
        reason="最近一次成功已是 ${age} 小时前，超过 ${max_age} 小时，定时任务可能没有按时触发"
      fi
    fi
  fi

  printf -v "${prefix}_job_state" '%s' "$job_state"
  printf -v "${prefix}_status" '%s' "$status"
  printf -v "${prefix}_stale" '%s' "$stale"
  printf -v "${prefix}_result" '%s' "$result"
  printf -v "${prefix}_exit_status" '%s' "$exit_status"
  printf -v "${prefix}_exit_at" '%s' "$exit_at"
  printf -v "${prefix}_age_hours" '%s' "$age"
  printf -v "${prefix}_reason" '%s' "$reason"
}

# 把 collect_unit_health 采集的结果格式化为 JSON 对象（单行）。
# 参数: $1 变量前缀；$2 单元名；$3 最大允许年龄（小时）。
# 返回: stdout 输出 JSON 对象。
unit_health_json() {
  local prefix="$1" unit="$2" max_age="$3" name ok=true
  local job_state status stale result exit_status exit_at age reason
  name="${prefix}_job_state"; job_state="${!name}"
  name="${prefix}_status"; status="${!name}"
  name="${prefix}_stale"; stale="${!name}"
  name="${prefix}_result"; result="${!name}"
  name="${prefix}_exit_status"; exit_status="${!name}"
  name="${prefix}_exit_at"; exit_at="${!name}"
  name="${prefix}_age_hours"; age="${!name}"
  name="${prefix}_reason"; reason="${!name}"
  [[ "$status" == ok ]] || ok=false
  [[ "$max_age" =~ ^[0-9]+$ ]] || max_age=-1
  printf '{"ok": %s, "status": "%s", "unit": "%s", "job_state": "%s", "stale": %s, "result": "%s", "exit_status": "%s", "last_exit_at": "%s", "age_hours": %s, "max_age_hours": %s, "reason": "%s"}' \
    "$ok" "$status" "$(json_escape "$unit")" "$job_state" "$stale" "$(json_escape "$result")" \
    "$(json_escape "$exit_status")" "$(json_escape "$exit_at")" "$age" "$max_age" "$(json_escape "$reason")"
}
