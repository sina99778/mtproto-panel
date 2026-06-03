#!/usr/bin/env bash
# =====================================================================
#  MTProto Panel - management & update helper
#      sudo bash /opt/mtproto-panel/manage.sh <command>
# =====================================================================
set -euo pipefail
BASE=/opt/mtproto-panel
SVC=mtproto-panel

log() { echo -e "\033[1;36m[*]\033[0m $*"; }
die() { echo -e "\033[1;31m[!]\033[0m $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "این دستور را با sudo اجرا کن."

case "${1:-help}" in
  update)
    [ -d "$BASE/.git" ] || die "این نصب از طریق git نیست؛ آپدیت خودکار ممکن نیست. با روش git دوباره نصب کن."
    git config --global --add safe.directory "$BASE" 2>/dev/null || true
    CUR=$(cat "$BASE/VERSION" 2>/dev/null || echo "?")
    log "دریافت آخرین تغییرات از گیت‌هاب (نسخهٔ فعلی: $CUR) ..."
    git -C "$BASE" pull --ff-only || {
      log "fast-forward ممکن نشد؛ ریست سخت به نسخهٔ ریموت (داده‌ها در data/ حفظ می‌شود) ..."
      git -C "$BASE" fetch --all
      git -C "$BASE" reset --hard "@{u}"
    }
    log "اجرای نصب‌کننده برای اعمال آپدیت (نصب وابستگی‌ها + مهاجرت دیتابیس + ری‌استارت) ..."
    bash "$BASE/install.sh"
    log "آپدیت کامل شد. نسخهٔ جدید: $(cat "$BASE/VERSION" 2>/dev/null || echo "?")"
    ;;

  status)
    systemctl status "$SVC" --no-pager || true
    echo
    echo "— پروکسی‌ها —"
    systemctl list-units 'mtproxy-*' 'mtrelay-*' --no-pager --all || true
    ;;

  restart) systemctl restart "$SVC"; log "پنل ری‌استارت شد." ;;
  logs)    journalctl -u "$SVC" -n 200 -f ;;

  backup)
    TS=$(date +%Y%m%d-%H%M%S)
    OUT="/root/mtproto-panel-backup-$TS.tar.gz"
    tar -czf "$OUT" -C "$BASE" data
    log "بکاپ ساخته شد: $OUT  (شامل دیتابیس + panel.env)"
    ;;

  version) cat "$BASE/VERSION" 2>/dev/null || echo "unknown" ;;

  uninstall)
    read -r -p "همهٔ سرویس‌ها حذف شوند؟ (yes/no) " a
    [ "$a" = "yes" ] || { echo "لغو شد."; exit 0; }
    systemctl disable --now "$SVC" 2>/dev/null || true
    for u in /etc/systemd/system/mtproxy-*.service /etc/systemd/system/mtrelay-*.service; do
      [ -e "$u" ] || continue
      n=$(basename "$u"); systemctl disable --now "$n" 2>/dev/null || true; rm -f "$u"
    done
    rm -f "/etc/systemd/system/$SVC.service"
    systemctl daemon-reload
    log "سرویس‌ها حذف شدند. پوشهٔ $BASE (و داده‌ها) دست‌نخورده ماند. برای حذف کامل: rm -rf $BASE"
    ;;

  *)
    cat <<EOF
استفاده:  sudo bash $BASE/manage.sh <command>

  update      دریافت آخرین نسخه از گیت‌هاب و اعمال آپدیت (داده‌ها حفظ می‌شود)
  status      وضعیت پنل و پروکسی‌ها
  restart     ری‌استارت پنل
  logs        نمایش زندهٔ لاگ پنل
  backup      بکاپ از data/ (دیتابیس + تنظیمات) در /root
  version     نمایش نسخهٔ نصب‌شده
  uninstall   حذف سرویس‌ها (داده‌ها باقی می‌ماند)
EOF
    ;;
esac
