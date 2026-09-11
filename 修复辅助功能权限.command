#!/bin/zsh
# 清除 微信导出.app 残留的「已拒绝」辅助功能权限记录。
# 必须在 Terminal.app 里运行（它有完全磁盘访问权限，SIP 才允许改 TCC.db）。

DB="/Library/Application Support/com.apple.TCC/TCC.db"
# 从脚本自身位置推出 App 路径，不写死用户名/路径
APP="$(cd "$(dirname "$0")" && pwd)/微信导出.app"

echo "== 修复前 =="
sudo sqlite3 "$DB" \
  "select client, auth_value from access
   where service='kTCCServiceAccessibility' and client like '${APP}%';"

echo
echo "== 删除记录 =="
sudo sqlite3 "$DB" \
  "delete from access
   where service='kTCCServiceAccessibility' and client like '${APP}%';" \
  && echo "已删除" || { echo "删除失败：请确认是在 Terminal.app 里运行本脚本"; exit 1; }

sudo killall tccd 2>/dev/null

echo
echo "== 修复后（应为空） =="
sudo sqlite3 "$DB" \
  "select client, auth_value from access
   where service='kTCCServiceAccessibility' and client like '${APP}%';"

echo
echo "完成。现在重新打开 微信导出.app："
echo "  弹窗请点【打开系统设置】，不要点【拒绝】，然后在辅助功能里勾上 微信导出。"
