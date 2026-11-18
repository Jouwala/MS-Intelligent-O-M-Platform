#!/bin/sh
# noc-paths.sh —— NOC_HOME 外部目录解析（ZIP 交付 Phase 1，P1-4）
# =============================================================================
# start.sh / stop.sh 共同点源（. 引入），只做路径解析，不启动、不停止任何东西。
# 与 ops/deploy/noc-paths.ps1 对标：同一套模式/变量/断言，改动这里 = 改动两处
# 行为，须经评审。要求：POSIX sh + GNU coreutils（readlink -f）。
#
# 两种模式（只看进程环境变量 NOC_HOME 是否设置）：
# · legacy（未设 NOC_HOME）：全部路径与旧行为一致，全部由 $ROOT 派生。
# · external（设了 NOC_HOME）：config/data/logs/runtime 一律在部署根下，
#   版本目录（$ROOT）在启动后零写入；写目标落在版本目录内会直接拒绝。
#
# NOC_HOME 与 NOC_*_DIR 只从进程环境读，写在 .env 里无效——因为 .env
# 文件本身的位置就靠它们定。由首次启动脚本/部署包装器在拉起前设置。
#
# Linux 运行时布局（与 runtime-build.yml 的解压布局一致）：
#   <RuntimeDir>/node/bin/node、python312/bin/python3、
#   postgresql16/bin/{initdb,pg_ctl,psql,createdb}、nginx-1.28.0/{sbin/,}nginx
# Python 解释器名按 python3 → python3.12 → python 依次探测（Linux 侧 Python
# 来源待定，见 Phase 0 附录 B；探测不到时预检报错，不静默回退系统解释器）。
# =============================================================================

# 规范化路径（解析 . / .. / 符号链接；不存在的路径也按词法归一）。
_np_canonical() {
  _np_out=$(readlink -f -- "$1" 2>/dev/null) || _np_out=$1
  printf '%s' "$_np_out"
}

# 边界感知的前缀判定：相等或落在子目录内才算“在下面”。
# 与 .ps1 的 Test-PathUnder 一致（Linux 区分大小写，不做大小写归一）。
np_path_under() {
  _np_base=$(_np_canonical "$1")
  _np_path=$(_np_canonical "$2")
  _np_base=${_np_base%/}
  _np_path=${_np_path%/}
  [ "$_np_path" = "$_np_base" ] && return 0
  case "$_np_path" in
    "$_np_base"/*) return 0 ;;
    *) return 1 ;;
  esac
}

# 进程环境变量非空则取它，否则取默认值（与 .ps1 的 Get-ProcessEnvOrDefault 一致）。
_np_env_or_default() {
  _np_v=$(printenv "$1" 2>/dev/null || true)
  _np_v=$(printf '%s' "$_np_v" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
  if [ -n "$_np_v" ]; then printf '%s' "$_np_v"; else printf '%s' "$2"; fi
}

# nginx 目录解析：按偏好顺序探测含可执行 nginx 的目录（sbin/nginx 或 nginx）。
_np_resolve_nginx_dir() {
  _np_root_nginx="$1/nginx-1.28.0"
  _np_runtime_nginx="$2/nginx-1.28.0"
  if [ "$3" = "1" ]; then _np_first="$_np_runtime_nginx"; _np_second="$_np_root_nginx";
  else _np_first="$_np_root_nginx"; _np_second="$_np_runtime_nginx"; fi
  for _np_c in "$_np_first" "$_np_second"; do
    if [ -x "$_np_c/sbin/nginx" ] || [ -x "$_np_c/nginx" ]; then printf '%s' "$_np_c"; return 0; fi
  done
  printf '%s' "$_np_first"
}

_np_resolve_nginx_exe() {
  if [ -x "$1/sbin/nginx" ]; then printf '%s' "$1/sbin/nginx";
  elif [ -x "$1/nginx" ]; then printf '%s' "$1/nginx";
  else printf '%s' "$1/sbin/nginx"; fi
}

_np_resolve_py_exe() {
  for _np_c in "$1/python312/bin/python3" "$1/python312/bin/python3.12" "$1/python312/bin/python"; do
    if [ -x "$_np_c" ]; then printf '%s' "$_np_c"; return 0; fi
  done
  printf '%s' "$1/python312/bin/python3"
}

# 主入口：noc_paths <Root>；设置 NP_* 变量。
noc_paths() {
  NP_ROOT=$(_np_canonical "$1")
  NP_ROOT=${NP_ROOT%/}
  _np_home=$(_np_env_or_default 'NOC_HOME' '')
  if [ -n "$_np_home" ]; then
    NP_MODE='external'
    NP_NOC_HOME=$(_np_canonical "$_np_home"); NP_NOC_HOME=${NP_NOC_HOME%/}
    NP_CONFIG_DIR=$(_np_env_or_default 'NOC_CONFIG_DIR' "$NP_NOC_HOME/config")
    NP_DATA_DIR=$(_np_env_or_default 'NOC_DATA_DIR' "$NP_NOC_HOME/data")
    NP_LOGS_DIR=$(_np_env_or_default 'NOC_LOGS_DIR' "$NP_NOC_HOME/logs")
    NP_RUNTIME_DIR=$(_np_env_or_default 'NOC_RUNTIME_DIR' "$NP_NOC_HOME/runtime")
    NP_ENV_FILE=$(_np_env_or_default 'NOC_ENV_FILE' "$NP_CONFIG_DIR/.env")
    NP_TMP_DIR=$NP_LOGS_DIR
    NP_PGDATA="$NP_DATA_DIR/pgdata"
    NP_AUTH_DIR="$NP_DATA_DIR/portal-auth-data"
    NP_BOT_DATA_DIR="$NP_DATA_DIR/bot-data"
    NP_NGINX_DIR=$(_np_resolve_nginx_dir "$NP_ROOT" "$NP_RUNTIME_DIR" 1)
    NP_NGINX_CONF_OUT="$NP_CONFIG_DIR/nginx.conf"
    NP_NGINX_LOGS_DIR="$NP_LOGS_DIR/nginx"
    NP_NGINX_TEMP_DIR="$NP_LOGS_DIR/nginx-temp"
    NP_CERTS_DIR="$NP_CONFIG_DIR/certs"
  else
    NP_MODE='legacy'
    NP_NOC_HOME=''
    NP_CONFIG_DIR=$NP_ROOT
    NP_DATA_DIR="$NP_ROOT/runtime"
    NP_LOGS_DIR="$NP_ROOT/TMP"
    NP_RUNTIME_DIR="$NP_ROOT/runtime"
    NP_ENV_FILE="$NP_ROOT/.env"
    NP_TMP_DIR="$NP_ROOT/TMP"
    NP_PGDATA="$NP_RUNTIME_DIR/pgdata"
    NP_AUTH_DIR="$NP_RUNTIME_DIR/portal-auth-data"
    NP_BOT_DATA_DIR="$NP_ROOT/apps/whatsapp-kb-bot/data"
    NP_NGINX_DIR=$(_np_resolve_nginx_dir "$NP_ROOT" "$NP_RUNTIME_DIR" 0)
    NP_NGINX_CONF_OUT="$NP_NGINX_DIR/conf/nginx.conf"
    NP_NGINX_LOGS_DIR="$NP_NGINX_DIR/logs"
    NP_NGINX_TEMP_DIR="$NP_NGINX_DIR/temp"
    NP_CERTS_DIR="$NP_NGINX_DIR/certs"
  fi
  NP_NODE_EXE="$NP_RUNTIME_DIR/node/bin/node"
  NP_PY_EXE=$(_np_resolve_py_exe "$NP_RUNTIME_DIR")
  NP_PG_BIN="$NP_RUNTIME_DIR/postgresql16/bin"
  NP_NGINX_EXE=$(_np_resolve_nginx_exe "$NP_NGINX_DIR")
}

# 外部模式断言：写目标不得落在版本目录内（与 .ps1 的 Assert-NocPathsOutsideRoot 同表）。
noc_assert_outside_root() {
  [ "$NP_MODE" = "external" ] || return 0
  _np_assert_one() {
    if np_path_under "$NP_ROOT" "$2"; then
      echo "[FAIL] 外部模式写目标落在版本目录内：$1 = $2（版本目录 $NP_ROOT），请检查 NOC_HOME / NOC_*_DIR" >&2
      return 1
    fi
    return 0
  }
  _np_assert_one 'NOC_HOME' "$NP_NOC_HOME" || return 1
  _np_assert_one 'NOC_CONFIG_DIR' "$NP_CONFIG_DIR" || return 1
  _np_assert_one 'NOC_DATA_DIR' "$NP_DATA_DIR" || return 1
  _np_assert_one 'NOC_LOGS_DIR' "$NP_LOGS_DIR" || return 1
  _np_assert_one 'NOC_RUNTIME_DIR' "$NP_RUNTIME_DIR" || return 1
  _np_assert_one 'NOC_ENV_FILE' "$NP_ENV_FILE" || return 1
  _np_assert_one 'PgData' "$NP_PGDATA" || return 1
  _np_assert_one 'AuthDir' "$NP_AUTH_DIR" || return 1
  _np_assert_one 'BotDataDir' "$NP_BOT_DATA_DIR" || return 1
  _np_assert_one 'NginxConfOut' "$NP_NGINX_CONF_OUT" || return 1
  _np_assert_one 'NginxLogsDir' "$NP_NGINX_LOGS_DIR" || return 1
  _np_assert_one 'NginxTempDir' "$NP_NGINX_TEMP_DIR" || return 1
  _np_assert_one 'CertsDir' "$NP_CERTS_DIR" || return 1
  return 0
}
