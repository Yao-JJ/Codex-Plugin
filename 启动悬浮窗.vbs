Option Explicit
Dim shell, fs, scriptPath
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
scriptPath = fs.BuildPath(fs.GetParentFolderName(WScript.ScriptFullName), "codex_monitor.py")
shell.Run "pythonw.exe """ & scriptPath & """", 0, False
