-- 「微信导出.app」的外壳：启动同目录下的 app.py 图形界面。
-- 用 `path to me` 推出项目目录，不写死用户名和路径——换电脑、改用户名、
-- 把整个文件夹搬到别处都能跑。
-- 注意：do shell script 必须是阻塞式的。加 & 后台化会让 open 报 -10669。
set myPath to POSIX path of (path to me)
set projDir to do shell script "cd " & quoted form of myPath & "/.. && pwd"
do shell script quoted form of (projDir & "/.venv/bin/python") & " " & ¬
    quoted form of (projDir & "/app.py") & " > " & ¬
    quoted form of (projDir & "/_applog.txt") & " 2>&1"
