#!/usr/bin/env bash
# 安装/升级 Linux 沙箱 Agent Worker（systemd）。幂等：重复执行只会重写单元并重启。
#
# 用法：
#   sudo ./install.sh [--config /etc/agent-worker-linux/config.json] [--no-start]
#
# 它做什么：
#   1) 校验 python3 >= 3.10、仓库 clone 完整；
#   2) 首次安装时从 config.example.json 取一台示例生成配置（chmod 600），
#      并**提示必须改 instance**（三台 VM 不能相同）；
#   3) 按本机路径渲染 agent-worker-linux.service 模板 → /etc/systemd/system/；
#   4) daemon-reload + enable；默认 start（--no-start 跳过，便于先 --check）；
#   5) 打印回滚步骤。原型 ~/workspace/agent-worker-linux 目录**不删除、不修改**。
set -euo pipefail

SERVICE_NAME="agent-worker-linux"
PACKAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$PACKAGE_DIR/../.." && pwd)"
EXAMPLE="$PACKAGE_DIR/config.example.json"
UNIT_TEMPLATE="$PACKAGE_DIR/agent-worker-linux.service"
CONFIG_DIR="/etc/agent-worker-linux"
CONFIG_PATH="$CONFIG_DIR/config.json"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
NO_START=0
CONFIG_OVERRIDE=""

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG_PATH="$2"; shift 2 ;;
    --no-start) NO_START=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 sudo 运行（需要写 /etc/systemd/system 与 $CONFIG_DIR）" >&2
  exit 1
fi

PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ]; then
  echo "未找到 python3" >&2
  exit 1
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "需要 Python >= 3.10，当前：$("$PYTHON" --version 2>&1)" >&2
  exit 1
fi
if [ ! -f "$REPO_ROOT/apps/control-plane/backend/scheduler/factory.py" ]; then
  echo "仓库 clone 不完整：缺少 apps/control-plane/backend/scheduler/factory.py" >&2
  echo "请把本包放在 <repo>/apps/agent-worker-linux/ 下再执行" >&2
  exit 1
fi

WORKER_USER="${SUDO_USER:-root}"
WORKER_GROUP="$(id -gn "$WORKER_USER" 2>/dev/null || echo "$WORKER_USER")"
WORKER_HOME="$(getent passwd "$WORKER_USER" 2>/dev/null | cut -d: -f6 || true)"
if [ -z "$WORKER_HOME" ]; then
  WORKER_HOME="$HOME"
fi

mkdir -p "$(dirname "$CONFIG_PATH")"
GENERATED_CONFIG=0
if [ ! -f "$CONFIG_PATH" ]; then
  if [ ! -f "$EXAMPLE" ]; then
    echo "缺少示例配置：$EXAMPLE" >&2
    exit 1
  fi
  "$PYTHON" - "$EXAMPLE" "$CONFIG_PATH" <<'PY'
import json
import sys

example_path, target = sys.argv[1], sys.argv[2]
with open(example_path, encoding="utf-8") as handle:
    examples = json.load(handle)
if not isinstance(examples, list) or not examples:
    raise SystemExit("config.example.json 必须是非空数组（三台 VM 示例）")
with open(target, "w", encoding="utf-8") as handle:
    json.dump(examples[0], handle, ensure_ascii=False, indent=2)
    handle.write("\n")
print(f"已生成配置：{target}（来自示例第一条）")
PY
  GENERATED_CONFIG=1
  chmod 600 "$CONFIG_PATH"
  chown "$WORKER_USER:$WORKER_GROUP" "$CONFIG_PATH" 2>/dev/null || true
fi

read -r STATE_DIR WORKDIR CONFIG_INSTANCE < <("$PYTHON" - "$CONFIG_PATH" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    cfg = json.load(handle)
workdir = cfg.get("workdir") or "~/workspace"
state_dir = cfg.get("state_dir") or f"{workdir}/agent-worker-linux-state"
print(state_dir, workdir, cfg.get("instance") or "")
PY
)
STATE_DIR="${STATE_DIR/#\~/$WORKER_HOME}"
WORKDIR="${WORKDIR/#\~/$WORKER_HOME}"
mkdir -p "$STATE_DIR" "$WORKDIR"
chown -R "$WORKER_USER:$WORKER_GROUP" "$STATE_DIR" "$WORKDIR" 2>/dev/null || true

RENDERED="$(mktemp)"
trap 'rm -f "$RENDERED"' EXIT
sed \
  -e "s|@@WORKER_USER@@|${WORKER_USER}|g" \
  -e "s|@@WORKER_GROUP@@|${WORKER_GROUP}|g" \
  -e "s|@@REPO_ROOT@@|${REPO_ROOT}|g" \
  -e "s|@@PYTHON@@|${PYTHON}|g" \
  -e "s|@@PACKAGE_DIR@@|${PACKAGE_DIR}|g" \
  -e "s|@@CONFIG_PATH@@|${CONFIG_PATH}|g" \
  -e "s|@@STATE_DIR@@|${STATE_DIR}|g" \
  -e "s|@@WORKDIR@@|${WORKDIR}|g" \
  "$UNIT_TEMPLATE" > "$RENDERED"

if [ -f "$UNIT_PATH" ]; then
  cp -p "$UNIT_PATH" "${UNIT_PATH}.bak.$(date +%Y%m%d%H%M%S)"
fi
install -m 644 "$RENDERED" "$UNIT_PATH"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null

if [ "$NO_START" -eq 1 ]; then
  echo "已安装但按 --no-start 未启动："
  echo "  sudo systemctl start $SERVICE_NAME"
else
  systemctl restart "$SERVICE_NAME"
  sleep 1
  systemctl --no-pager --lines=5 status "$SERVICE_NAME" || true
fi

echo
echo "================ 安装信息 ================"
echo "包目录        : $PACKAGE_DIR"
echo "仓库根        : $REPO_ROOT"
echo "配置          : $CONFIG_PATH"
echo "systemd 单元  : $UNIT_PATH"
echo "运行用户      : $WORKER_USER:$WORKER_GROUP"
if [ "$GENERATED_CONFIG" -eq 1 ]; then
  echo
  echo "⚠ 首次生成配置：请**先改 instance**（当前示例值：${CONFIG_INSTANCE:-未知}）"
  echo "  三台 VM 必须各不相同（如 muse-1-linux / muse-2-linux / muse-3-linux），"
  echo "  并确认 repo_root / workdir / token 来源后："
  echo "    sudo systemctl restart $SERVICE_NAME"
fi
echo
echo "================ 回滚步骤 ================"
echo "  sudo systemctl disable --now $SERVICE_NAME"
echo "  sudo rm -f $UNIT_PATH && sudo systemctl daemon-reload"
echo "  # 恢复原型服务（旧目录未改动）：按原型原有 systemd 单元/命令启动"
echo "  # 旧配置与状态目录均保留，可随时切回"
