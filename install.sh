#!/usr/bin/env bash
# =====================================================================
#  MTProto Panel - installer & updater for Ubuntu 20.04/22.04/24.04
#
#  First install (recommended, git -> easy updates):
#      sudo apt-get update && sudo apt-get install -y git
#      sudo git clone https://github.com/sina99778/mtproto-panel /opt/mtproto-panel
#      cd /opt/mtproto-panel && sudo bash install.sh
#
#  Update later:
#      sudo bash /opt/mtproto-panel/manage.sh update
#
#  This script is idempotent: re-running it updates code/deps and restarts
#  the service WITHOUT touching data/ (database + panel.env) or venv.
# =====================================================================
set -euo pipefail

BASE=/opt/mtproto-panel
PANEL_WEB_PORT=8088
ENGINE_REPO="https://github.com/alexbers/mtprotoproxy.git"

log() { echo -e "\033[1;36m[*]\033[0m $*"; }
die() { echo -e "\033[1;31m[!]\033[0m $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "این اسکریپت باید با دسترسی root اجرا شود (sudo bash install.sh)."

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- 1. System dependencies ----------------------------------------------
log "نصب پیش‌نیازهای سیستمی ..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git curl rsync ufw ca-certificates gzip

# --- 2. Get the code into $BASE ------------------------------------------
git config --global --add safe.directory "$BASE" 2>/dev/null || true
if [ "$SRC_DIR" = "$BASE" ]; then
  log "اجرا در محل نصب ($BASE)."
elif [ -n "${REPO_URL:-}" ]; then
  if [ -d "$BASE/.git" ]; then
    log "آپدیت کد از گیت ($REPO_URL) ..."
    git -C "$BASE" pull --ff-only || { git -C "$BASE" fetch --all; git -C "$BASE" reset --hard "@{u}"; }
  else
    [ -d "$BASE" ] && [ -n "$(ls -A "$BASE" 2>/dev/null)" ] && die "$BASE خالی نیست و git repo هم نیست؛ دستی پاکش کن یا روش git را استفاده کن."
    log "کلون پروژه به $BASE ..."
    git clone "$REPO_URL" "$BASE"
  fi
elif [ -f "$SRC_DIR/requirements.txt" ]; then
  log "کپی کد محلی به $BASE (بدون پشتیبانی آپدیت گیتی) ..."
  mkdir -p "$BASE"
  rsync -a --exclude venv --exclude data --exclude engine --exclude '.git' "$SRC_DIR"/ "$BASE"/
else
  die "منبع کد پیدا نشد. داخل کلون پروژه اجرا کن، یا REPO_URL را تنظیم کن."
fi

# --- 3. Python virtualenv -------------------------------------------------
log "ساخت/به‌روزرسانی محیط مجازی پایتون ..."
python3 -m venv "$BASE/venv"
"$BASE/venv/bin/pip" install --upgrade pip wheel >/dev/null
"$BASE/venv/bin/pip" install -r "$BASE/requirements.txt"
# Engine extras (faster crypto + event loop for the proxy & relay).
"$BASE/venv/bin/pip" install cryptography uvloop || true

# --- 4. Proxy engine (mtprotoproxy) --------------------------------------
log "دریافت/به‌روزرسانی موتور پروکسی (mtprotoproxy) ..."
mkdir -p "$BASE/engine"
if [ -d "$BASE/engine/mtprotoproxy/.git" ]; then
  git -C "$BASE/engine/mtprotoproxy" pull --ff-only || true
else
  git clone --depth 1 "$ENGINE_REPO" "$BASE/engine/mtprotoproxy"
fi

# --- 4b. Operator/ASN database for stats (DB-IP ASN Lite, best effort) ----
ASN_DB="$BASE/data/dbip-asn-lite.mmdb"
mkdir -p "$BASE/data"
if [ ! -f "$ASN_DB" ]; then
  log "دریافت دیتابیس ASN برای تشخیص اپراتور (اختیاری) ..."
  CUR=$(date +%Y-%m); PREV=$(date -d "last month" +%Y-%m 2>/dev/null || echo "$CUR")
  for M in "$CUR" "$PREV"; do
    if curl -fsSL "https://download.db-ip.com/free/dbip-asn-lite-$M.mmdb.gz" -o /tmp/asn.mmdb.gz 2>/dev/null; then
      gunzip -f /tmp/asn.mmdb.gz && mv /tmp/asn.mmdb "$ASN_DB" && log "دیتابیس ASN نصب شد ($M)." && break
    fi
  done
  [ -f "$ASN_DB" ] || log "دریافت دیتابیس ASN ناموفق بود؛ آمار اپراتور 'نامشخص' می‌ماند (بقیه آمار کار می‌کند)."
fi

# --- 5. Network tuning: BBR + sysctl -------------------------------------
log "اعمال بهینه‌سازی شبکه (BBR + sysctl) ..."
install -m 0644 "$BASE/sysctl/99-mtproto-panel.conf" /etc/sysctl.d/99-mtproto-panel.conf
modprobe tcp_bbr 2>/dev/null || true
echo "tcp_bbr" > /etc/modules-load.d/mtproto-bbr.conf
sysctl --system >/dev/null 2>&1 || true
CC=$(cat /proc/sys/net/ipv4/tcp_congestion_control 2>/dev/null || echo "?")
log "الگوریتم کنترل ازدحام فعلی: $CC"

# --- 6. Panel environment (generated once, preserved on update) ----------
ENV_FILE="$BASE/data/panel.env"
NEW_INSTALL=0
if [ ! -f "$ENV_FILE" ]; then
  NEW_INSTALL=1
  SECRET_KEY=$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 48)
  ADMIN_PASS=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 16)
  cat > "$ENV_FILE" <<EOF
PANEL_BASE_DIR=$BASE
PANEL_SECRET_KEY=$SECRET_KEY
PANEL_ADMIN_USER=admin
PANEL_ADMIN_PASSWORD=$ADMIN_PASS
PANEL_HOST=0.0.0.0
PANEL_PORT=$PANEL_WEB_PORT
PANEL_VENV_PYTHON=$BASE/venv/bin/python3
PANEL_ENGINE_SCRIPT=$BASE/engine/mtprotoproxy/mtprotoproxy.py
PANEL_RELAY_SCRIPT=$BASE/app/relay.py
EOF
  chmod 600 "$ENV_FILE"
fi

# --- 7. systemd service for the panel ------------------------------------
log "نصب/به‌روزرسانی سرویس systemd ..."
sed "s#__BASE__#$BASE#g" "$BASE/systemd/mtproto-panel.service" > /etc/systemd/system/mtproto-panel.service
chmod +x "$BASE/manage.sh" "$BASE/install.sh" 2>/dev/null || true
systemctl daemon-reload
systemctl enable mtproto-panel.service >/dev/null 2>&1 || true
systemctl restart mtproto-panel.service

# --- 8. Firewall ----------------------------------------------------------
if command -v ufw >/dev/null; then
  ufw allow 22/tcp                >/dev/null 2>&1 || true
  ufw allow "$PANEL_WEB_PORT"/tcp >/dev/null 2>&1 || true
fi

# --- Done -----------------------------------------------------------------
VER=$(cat "$BASE/VERSION" 2>/dev/null || echo "?")
IP=$(curl -fsSL https://api.ipify.org 2>/dev/null || echo "YOUR_SERVER_IP")
echo
echo "=========================================================="
if [ "$NEW_INSTALL" = "1" ]; then
  echo "  ✅ پنل MTProto نصب شد (نسخهٔ $VER)"
  echo "  آدرس پنل : http://$IP:$PANEL_WEB_PORT"
  echo "  کاربر     : admin"
  echo "  رمز عبور  : $ADMIN_PASS   (حتماً بعد از ورود تغییر بده)"
else
  echo "  ✅ پنل MTProto به‌روزرسانی شد (نسخهٔ $VER)"
  echo "  آدرس پنل : http://$IP:$PANEL_WEB_PORT"
fi
echo "  وضعیت BBR : $CC"
echo "----------------------------------------------------------"
echo "  آپدیت بعدی :  sudo bash $BASE/manage.sh update"
echo "  وضعیت/لاگ  :  sudo bash $BASE/manage.sh status | logs"
echo "=========================================================="
