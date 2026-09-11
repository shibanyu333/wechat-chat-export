#!/bin/zsh
# 生成桌面 App「微信导出.app」。
# 仓库里不带这个 .app：它是编译产物(二进制)，而且旧版里写死过打包者的用户名路径。
# 双击本脚本即可在同目录生成，几秒钟的事。
cd "$(dirname "$0")" || exit 1
rm -rf "微信导出.app"
osacompile -o "微信导出.app" 启动器.applescript || { echo "编译失败"; exit 1; }
# 换上自带图标(icon.icns 是纯图片资源，不含任何路径)
[ -f icon.icns ] && cp icon.icns "微信导出.app/Contents/Resources/applet.icns"
echo "✅ 已生成 微信导出.app"
echo "   · 自己编译的 App 没有开发者签名，首次双击若提示「来自身份不明的开发者」，"
echo "     请【右键 → 打开 → 再点打开】，之后就能正常双击。"
echo "   · 首次抓取需在 系统设置→隐私与安全性 的【辅助功能】和【屏幕录制】里勾上「微信导出」。"
