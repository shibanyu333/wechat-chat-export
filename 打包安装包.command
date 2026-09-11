#!/bin/zsh
# 一键打包：源码 → 独立 .app → 可分发的 .dmg 安装包
# 产物在 dist/ 下，不入 git（二进制太大，走 GitHub Releases 发布）
set -e
cd "$(dirname "$0")"

VERSION="${1:-1.0.0}"
APP="微信聊天记录导出.app"
# 文件名用 ASCII：GitHub Releases 会把资产名里的中文字符剥掉
# (实测「微信聊天记录导出-1.0.0.dmg」上传后变成「-1.0.0.dmg」)
DMG="dist/WeChatChatExport-${VERSION}.dmg"

echo "▶ 1/3 检查打包工具"
./.venv/bin/python -c "import PyInstaller" 2>/dev/null || ./.venv/bin/pip install -q pyinstaller

echo "▶ 2/3 打包独立 App（约 1~2 分钟）"
rm -rf build dist
./.venv/bin/python -m PyInstaller 打包.spec --noconfirm --clean >/dev/null
[ -d "dist/$APP" ] || { echo "✗ 打包失败"; exit 1; }

echo "▶ 3/3 制作 DMG 安装包"
STAGE="$(mktemp -d)"
cp -R "dist/$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"          # 让用户直接拖进去
cp "dist/$APP/Contents/Resources/THIRD_PARTY_LICENSES.txt" "$STAGE/第三方许可证.txt"
cat > "$STAGE/先读我.txt" <<'TXT'
安装：把「微信聊天记录导出」拖到右边的 Applications 文件夹。

首次打开：本 App 没有花钱买苹果开发者签名，系统会拦一下。
  右键点 App → 打开 → 在弹窗里再点一次「打开」。之后就能正常双击。

首次抓取还需要授权两项（系统会自动弹窗提示）：
  系统设置 → 隐私与安全性 → 辅助功能    → 勾上「微信聊天记录导出」
  系统设置 → 隐私与安全性 → 屏幕录制    → 勾上「微信聊天记录导出」
授权后请把 App 退出重开一次。

导出结果存放位置：
  ~/Documents/微信聊天记录导出/导出结果/

本工具全程本地运行、不联网，不解密微信数据库、不修改微信。

本工具是个人开发的开源软件，不是腾讯官方产品，与腾讯公司无关；
「微信」是腾讯公司的商标。源码与说明：
  https://github.com/shibanyu333/wechat-chat-export
TXT
hdiutil create -volname "微信聊天记录导出" -srcfolder "$STAGE" \
  -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

echo
echo "✅ 完成"
echo "   App : dist/$APP    ($(du -sh "dist/$APP" | cut -f1))"
echo "   安装包: $DMG   ($(du -sh "$DMG" | cut -f1))"
