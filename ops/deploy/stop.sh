#!/bin/sh
# stop.sh —— 只停「本副本」启动的服务（ZIP 交付 Phase 1，P1-4）
# =============================================================================
# 对标 ops/deploy/stop_all.ps1。入口：副本根目录的 stop.sh（薄壳，只调本脚本）。
#
# 安全原则（与 .ps1 版一致）：
#   · 只认 start.sh 写在 <TmpDir>/portable-<name>.pid 的 pid 文件；
#   · 每个 pid 都必须同时通过「进程名 + 启动时间」校验才动手
#     （pid 会被系统复用，只比 pid 会误杀无关进程）；
#   · 绝不按端口盲杀、绝不 tree kill、绝不碰计划任务 / watchdog。
#   · 停止用 TERM，5 秒未退再 KILL（.ps1 版直接强制终止；Linux 下先给
#     uvicorn/vite/tsx 优雅退出的机会，安全保证相同）。
# 因此：在别的机器上没跑过 start.sh 就调本脚本，结果应当是「无需停止」。
#
# nginx 特殊：优先用它自己的 -s quit 优雅退出（会读副本 logs/nginx.pid），
# pid 文件只作兜底。便携 PG 用 pg_ctl -m fast 停，只针对副本自己的 pgdata。
# P1-6 外部目录：设了 NOC_HOME 时，pid 文件、pgdata、nginx 配置/日志一律走
# 外部目录（解析见 noc-paths.sh，与 start.sh 共用）；不设则与旧行为一致。
#
# 用法：
#   ./ops/deploy/stop.sh [--dry-run]
# =============================================================================
set -u

DRY_RUN=0
for _a in "$@"; do
  case "$_a" in
    --dry-run) DRY_RUN=1 ;;
    --help|-h) echo "用法: $0 [--dry-run]"; exit 0 ;;
    *) echo "[FAIL] 未知参数: $_a（用法: $0 [--dry-run]）" >&2; exit 2 ;;
  esac
done

_self=$0
case "$_self" in */*) ;; *) _self=$(command -v -- "$_self" || printf '%s' "$_self") ;; esac
SCRIPT_DIR=$(dirname "$(readlink -f -- "$_self")")
ROOT=$(dirname "$(dirname "$SCRIPT_DIR")")

# 目录解析集中在 noc-paths.sh（与 start.sh 共用，唯一真相源）；
# 缺失则拒绝停止——静默回退会让外部模式停错地方。
NOC_PATHS_LIB="$SCRIPT_DIR/noc-paths.sh"
if [ ! -f "$NOC_PATHS_LIB" ]; then echo "[FAIL] 缺少目录解析脚本: $NOC_PATHS_LIB（请从仓库重新取 ops/deploy/）" >&2; exit 1; fi
# shellcheck disable=SC1091
. "$NOC_PATHS_LIB"
noc_paths "$ROOT"
if [ "$NP_MODE" = "external" ]; then noc_assert_outside_root || exit 1; fi
TMP_DIR=$NP_TMP_DIR
RUNTIME_DIR=$NP_RUNTIME_DIR
NGINX_EXE=$NP_NGINX_EXE
NGINX_DIR=$NP_NGINX_DIR
PGDATA=$NP_PGDATA
PG_CTL="$NP_PG_BIN/pg_ctl"

step() { echo "[*] $1"; }
ok() { echo "[OK] $1"; }
warn() { echo "[WARN] $1"; }
skip() { echo "[SKIP] $1"; }

pid_file() { printf '%s' "$TMP_DIR/portable-$1.pid"; }

# 读 /proc/<pid>/stat 的启动时间（字段 22，clock ticks since boot）。
# comm 字段在括号里且可能含空格，先截到最后一个 ')' 再数（之后第 20 个字段）。
proc_starttime() {
  _ps_stat=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
  _ps_after=${_ps_stat##*) }
  # shellcheck disable=SC2086
  set -- $_ps_after
  printf '%s' "${20}"
}

# 进程是否已不存在（含僵尸态：/proc 仍在但已死，须按已死处理）。
_proc_gone() {
  _pg_pid=$1
  [ -d "/proc/$_pg_pid" ] || return 0
  _pg_stat=$(cat "/proc/$_pg_pid/stat" 2>/dev/null) || return 0
  _pg_after=${_pg_stat##*) }
  # shellcheck disable=SC2086
  set -- $_pg_after
  [ "$1" = "Z" ]
}

# 返回「可信 pid」或 0。三重校验：pid 存在 + 进程名一致 + 启动时间一致。
trusted_pid() {
  _tp_pf=$(pid_file "$1")
  [ -f "$_tp_pf" ] || { printf '0'; return 0; }
  _tp_raw=$(cat "$_tp_pf" 2>/dev/null || true)
  _tp_pid=${_tp_raw%%|*}
  _tp_rest=${_tp_raw#*|}
  _tp_comm=${_tp_rest%%|*}
  _tp_ticks=${_tp_rest#*|}
  case "$_tp_pid" in ''|*[!0-9]*) printf '0'; return 0 ;; esac
  case "$_tp_ticks" in ''|*[!0-9]*) printf '0'; return 0 ;; esac
  _proc_gone "$_tp_pid" && { printf '0'; return 0; }
  _tp_now_comm=$(cat "/proc/$_tp_pid/comm" 2>/dev/null || true)
  [ "$_tp_now_comm" = "$_tp_comm" ] || { printf '0'; return 0; }
  _tp_now_ticks=$(proc_starttime "$_tp_pid" || true)
  [ "$_tp_now_ticks" = "$_tp_ticks" ] || { printf '0'; return 0; }
  printf '%s' "$_tp_pid"
}

stop_one() {
  _so_pid=$(trusted_pid "$1")
  if [ "$_so_pid" = "0" ]; then
    skip "$1 无本副本的存活进程"
  else
    if [ "$DRY_RUN" = "1" ]; then
      step "<干跑> 将停止 $1 (pid=$_so_pid)"
    else
      kill -TERM "$_so_pid" 2>/dev/null || true
      _so_wait=0
      while ! _proc_gone "$_so_pid" && [ "$_so_wait" -lt 10 ]; do sleep 0.5; _so_wait=$((_so_wait + 1)); done
      if ! _proc_gone "$_so_pid"; then kill -KILL "$_so_pid" 2>/dev/null || true; sleep 0.5; fi
      if ! _proc_gone "$_so_pid"; then warn "$1 (pid=$_so_pid) 停止失败";
      else ok "$1 已停止 (pid=$_so_pid)"; fi
    fi
  fi
  if [ "$DRY_RUN" != "1" ]; then rm -f "$(pid_file "$1")"; fi
}

step "停止副本：$ROOT"
echo ''

# ------------------------------------------------------ 1) nginx 优雅退出
step '--- 1/7 nginx ---'
NGINX_PID_FILE="$NP_NGINX_LOGS_DIR/nginx.pid"
if [ -x "$NGINX_EXE" ] && [ -f "$NGINX_PID_FILE" ]; then
  # 先校验 nginx.pid 指向的进程确属副本（exe 在副本根/运行时目录下），再发 -s quit；
  # stale pid 可能被系统复用，盲发信号会误伤无关进程。
  _npid=$(tr -d '[:space:]' < "$NGINX_PID_FILE")
  _trusted=0
  case "$_npid" in ''|*[!0-9]*) ;; *)
    if [ -d "/proc/$_npid" ]; then
      _exe=$(readlink "/proc/$_npid/exe" 2>/dev/null || true)
      if [ -n "$_exe" ] && { np_path_under "$ROOT" "$_exe" || np_path_under "$RUNTIME_DIR" "$_exe"; }; then _trusted=1; fi
    fi
    ;;
  esac
  if [ "$_trusted" = "1" ]; then
    if [ "$DRY_RUN" = "1" ]; then step '<干跑> 将执行 nginx -s quit';
    else
      # -c 绝对路径：外部模式配置在 config/ 下；便携模式与默认 conf 相同文件。
      "$NGINX_EXE" -s quit -p "$NGINX_DIR/" -c "$NP_NGINX_CONF_OUT" 2>&1 | sed 's/^/  /'
      ok 'nginx -s quit 已发出'
      sleep 0.8
    fi
  else skip 'nginx.pid 不可信（进程不存在或不属于副本），跳过 -s quit'; fi
else skip 'nginx 未运行（无 nginx.pid）'; fi
stop_one 'nginx'

# ------------------------------------------------ 2~6) 应用进程，按启动逆序
for _name in wa_bot wa_analysis portal frontend backend; do
  step "--- $_name ---"
  stop_one "$_name"
done

# --------------------------------------------------------- 7) 便携 PG
step '--- 7/7 便携 PostgreSQL ---'
if [ -x "$PG_CTL" ] && [ -f "$PGDATA/PG_VERSION" ]; then
  if [ "$DRY_RUN" = "1" ]; then step '<干跑> 将执行 pg_ctl stop -m fast';
  else
    if "$PG_CTL" -D "$PGDATA" status >/dev/null 2>&1; then
      # 不用管道捕获输出：长驻进程继承管道句柄会把调用方挂死（见 start_all.ps1 注释）。
      if "$PG_CTL" -D "$PGDATA" -m fast stop; then ok '便携 PG 已停止'; else warn 'pg_ctl stop 返回非零'; fi
    else skip '便携 PG 未运行'; fi
  fi
else skip '便携 PG 未初始化 / 缺 pg_ctl'; fi

echo ''
ok '完成：只处理了由 start.sh 记录的本副本进程，未触碰任何生产实例。'
