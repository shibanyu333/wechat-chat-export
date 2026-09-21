#!/bin/zsh
# 升级到新版之后，系统设置里明明勾着「微信聊天记录导出」，程序却还说缺少权限？
#
# 原因：本 App 没有花钱买苹果开发者签名（$99/年），每次重新打包，签名都是新的。
# macOS 的权限库(TCC)记的是【旧版那份签名】，新版对不上号 —— 界面上那个勾是旧记录
# 留下的假象，点开也没用。把旧记录清掉、重新授权一次就好了。
#
# 双击本文件即可，不需要 sudo。
BID="com.shibanyu333.wechatchatexport"
APP="/Applications/微信聊天记录导出.app"

echo "▶ 1/3 关掉正在运行的 App"
pkill -f "微信聊天记录导出.app/Contents/MacOS/WeChatExport" 2>/dev/null
sleep 1

echo "▶ 2/3 清掉旧的权限记录"
tccutil reset Accessibility "$BID" 2>/dev/null
tccutil reset ScreenCapture "$BID" 2>/dev/null

echo "▶ 3/3 重新打开 App"
if [ -d "$APP" ]; then
  open "$APP"
  echo
  echo "✅ 好了。现在点「开始抓取」，系统会重新弹窗要权限，按提示勾上即可："
  echo "     系统设置 → 隐私与安全性 → 辅助功能"
  echo "     系统设置 → 隐私与安全性 → 屏幕录制"
  echo "   要是列表里还留着同名的旧项，选中它按下面的「−」删掉，再重新勾一次。"
else
  echo
  echo "! 没在 /Applications 里找到「微信聊天记录导出.app」。"
  echo "  请先打开 DMG、把 App 拖进 Applications，再运行本脚本。"
fi
echo
read "?按回车关闭此窗口..."
