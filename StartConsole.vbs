Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' 固定到脚本所在目录
WshShell.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)

' 保证日志目录存在（尽管现在窗口可见，仍然保留日志）
If Not fso.FolderExists("logs") Then fso.CreateFolder("logs")

' 把 0（隐藏）改为 1（显示窗口），让用户能看到服务在跑，关掉窗口就停止
WshShell.Run "cmd /c title Tool to Markdown Console & python scripts\review_ui.py", 1, False
