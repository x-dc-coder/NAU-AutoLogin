#!/usr/bin/env bash
# ========================================================
# NAU-AutoLogin Linux 幂等安装/升级脚本 (树莓派 dc-pi / Debian, 需 systemd)
#
# 用法:
#   sudo ./deploy/install.sh                                  # 从脚本所在仓库目录安装 (开发/scp 目录场景)
#   sudo ./deploy/install.sh nau-autologin-1.0.0-linux.tar.gz # 从 CI Release 包安装
#   sudo ./deploy/install.sh <源> --user <运行用户>            # 指定守护进程运行用户 (默认取 SUDO_USER)
#
# 目录布局:
#   /opt/nau-autologin/releases/<ver>/   各版本完整解包
#   /opt/nau-autologin/current -> releases/<ver>   原子软链, 失败自动回滚
#   ~/.nau-autologin/{config.ini,.env}             配置与凭据 (跨版本保留, 不覆盖)
#
# 幂等性: 重复执行安全; 配置/凭据/单元只在缺失时写入; 升级仅切换软链。
# ========================================================
set -euo pipefail

SERVICE_NAME="nau-autologin"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-/opt/nau-autologin}"

log()  { echo "[install] $*"; }
warn() { echo "[install][WARN] $*" >&2; }
die()  { echo "[install][ERROR] $*" >&2; exit 1; }

# ---------- 参数解析 ----------
SRC=""
APP_USER="${SUDO_USER:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --user)    [ $# -ge 2 ] || die "--user 需要参数"; APP_USER="$2"; shift 2 ;;
    --user=*)  APP_USER="${1#*=}"; shift ;;
    -h|--help) grep '^#' "$0" | tail -n +2 | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) if [ -z "$SRC" ]; then SRC="$1"; shift; else die "多余参数: $1"; fi ;;
  esac
done

# ---------- 前置检查 ----------
[ "$(id -u)" -eq 0 ] || die "请用 sudo 运行"
command -v systemctl >/dev/null || die "未检测到 systemd"
command -v python3   >/dev/null || die "未检测到 python3"
if [ -z "$APP_USER" ]; then
  warn "未指定 --user 且无 SUDO_USER, 守护进程将以 root 运行 (不推荐)"
  APP_USER="root"
fi
id "$APP_USER" >/dev/null || die "运行用户不存在: $APP_USER"
APP_GROUP="$(id -gn "$APP_USER")"
USER_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
[ -n "$USER_HOME" ] || die "无法解析 $APP_USER 的家目录"
CFG_DIR="$USER_HOME/.nau-autologin"

# ---------- 1. 解包 / 复制到 releases/<ver> ----------
if [ -z "$SRC" ]; then
  SRC="$SCRIPT_DIR/.."
fi
if [ -f "$SRC" ]; then
  case "$SRC" in *.tar.gz|*.tgz) ;; *) die "不支持的包格式: $SRC (需要 .tar.gz)" ;; esac
  VER="$(basename "$SRC")"; VER="${VER%.tar.gz}"; VER="${VER%.tgz}"
  VER="${VER#nau-autologin-}"; VER="${VER%-linux}"
  MODE="tarball"
elif [ -d "$SRC" ]; then
  [ -f "$SRC/session_manager.py" ] || die "目录 $SRC 不含 session_manager.py, 请指向仓库根"
  VER="dev-$(date +%Y%m%d%H%M%S)"
  MODE="dir"
else
  die "源不存在: $SRC (传 .tar.gz 包或仓库目录)"
fi

log "安装源: $SRC ($MODE, 版本 $VER)"
mkdir -p "$APP_DIR/releases"
STAGE="$(mktemp -d "$APP_DIR/releases/.stage-XXXXXX")"

if [ "$MODE" = "tarball" ]; then
  tar -xzf "$SRC" -C "$STAGE"
  # 兼容两种打包布局: 仓库根平铺, 或包内单一顶层目录
  # (dotglob: 点文件如 .env.example 必须随目录平移)
  shopt -s nullglob
  entries=("$STAGE"/*)
  if [ "${#entries[@]}" -eq 1 ] && [ -d "${entries[0]}" ]; then
    shopt -s dotglob
    mv "${entries[0]}"/* "$STAGE"/
    rmdir "${entries[0]}"
  fi
  shopt -u nullglob dotglob
else
  (cd "$SRC" && tar -cf - \
    --exclude=.git --exclude=__pycache__ --exclude='*.pyc' \
    --exclude=.venv --exclude=.env --exclude='*.log' --exclude='*.tar.gz' .) \
    | tar -xf - -C "$STAGE"
fi
[ -f "$STAGE/session_manager.py" ]     || { rm -rf "$STAGE"; die "包内容缺少 session_manager.py"; }
[ -f "$STAGE/linux/nau_keepalive.py" ] || { rm -rf "$STAGE"; die "包内容缺少 linux/nau_keepalive.py"; }
[ -f "$STAGE/linux/config.ini.example" ] || { rm -rf "$STAGE"; die "包内容缺少 linux/config.ini.example"; }

REL="$APP_DIR/releases/$VER"
rm -rf "$REL"          # 同版本重装 = 覆盖 (正在运行的旧实例靠已打开的文件句柄存活到 restart)
mv "$STAGE" "$REL"
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR"

# ---------- 2. venv (纯标准库时跳过) ----------
PY="$(command -v python3)"
if [ -f "$REL/requirements.txt" ]; then
  log "检测到 requirements.txt, 创建 venv 并安装依赖 (需 python3-venv)"
  python3 -m venv "$REL/.venv"
  "$REL/.venv/bin/pip" install --upgrade pip >/dev/null
  "$REL/.venv/bin/pip" install -r "$REL/requirements.txt"
  PY="$REL/.venv/bin/python3"
else
  log "纯标准库实现, 跳过 venv (仅当出现第三方依赖时才需要 python3-venv)"
fi

# ---------- 3. 原子切换 current 软链 ----------
PREV_REL=""
if [ -L "$APP_DIR/current" ]; then
  PREV_REL="$(readlink "$APP_DIR/current")"
  case "$PREV_REL" in /*) ;; *) PREV_REL="$APP_DIR/$PREV_REL" ;; esac
fi
LNK="$APP_DIR/.current.new"
ln -sfn "$REL" "$LNK"
mv -Tf "$LNK" "$APP_DIR/current"
log "current -> $(readlink "$APP_DIR/current")"

# ---------- 4. 写配置与凭据 (只写缺失项, 永不覆盖) ----------
mkdir -p "$CFG_DIR"
chown "$APP_USER:$APP_GROUP" "$CFG_DIR"
chmod 0700 "$CFG_DIR"
if [ ! -f "$CFG_DIR/config.ini" ]; then
  install -m 0644 -o "$APP_USER" -g "$APP_GROUP" "$REL/linux/config.ini.example" "$CFG_DIR/config.ini"
  log "已写入默认配置: $CFG_DIR/config.ini"
else
  log "配置已存在, 保持不变: $CFG_DIR/config.ini"
fi
if [ ! -f "$CFG_DIR/.env" ]; then
  install -m 0600 -o "$APP_USER" -g "$APP_GROUP" "$REL/.env.example" "$CFG_DIR/.env"
  warn "已生成凭据模板: $CFG_DIR/.env —— 请立即填写 NAU_USERNAME / NAU_PASSWORD 后重新执行本脚本!"
else
  log "凭据文件已存在, 保持不变: $CFG_DIR/.env"
fi

# ---------- 5. systemd 单元 + 启动 ----------
sed -e "s|__APP_USER__|$APP_USER|g" \
    -e "s|__APP_DIR__|$APP_DIR|g" \
    -e "s|__PYTHON__|$PY|g" \
    "$SCRIPT_DIR/nau-autologin.service" > "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
systemctl restart "$SERVICE_NAME"

# ---------- 6. 健康检查 (失败自动回滚) ----------
sleep 3
HEALTH_OK=1
systemctl is-active --quiet "$SERVICE_NAME" || HEALTH_OK=0
if [ "$HEALTH_OK" -eq 1 ]; then
  log "服务已 active, 以 $APP_USER 运行单轮检测 (--once, 在 i-NAU 网内会真实登录一次)..."
  if runuser -u "$APP_USER" -- env HOME="$USER_HOME" \
      "$PY" "$REL/linux/nau_keepalive.py" --once; then
    log "单轮检测通过: 外网连通或门户登录成功"
  else
    warn "单轮检测未通过 (本机不在 i-NAU 网内属预期); 服务保持运行, 将按周期自动重试"
  fi
  log "安装完成: journalctl -u $SERVICE_NAME -f 查看实时日志"
else
  warn "服务未进入 active 状态, 最近日志:"
  journalctl -u "$SERVICE_NAME" -n 30 --no-pager || true
  if [ -n "$PREV_REL" ] && [ -d "$PREV_REL" ] && [ "$PREV_REL" != "$REL" ]; then
    warn "自动回滚到上一版本: $PREV_REL"
    ln -sfn "$PREV_REL" "$LNK"
    mv -Tf "$LNK" "$APP_DIR/current"
    systemctl restart "$SERVICE_NAME"
    sleep 3
    if systemctl is-active --quiet "$SERVICE_NAME"; then
      log "回滚成功, 旧版本继续服务; 新版本 $REL 已保留在 releases/ 供排查"
    else
      die "回滚后服务仍异常, 请排查: journalctl -u $SERVICE_NAME -e"
    fi
  else
    die "无上一版本可回滚, 请排查: journalctl -u $SERVICE_NAME -e"
  fi
fi
