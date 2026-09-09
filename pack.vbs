' Launcher: runs pack.bat in a persistent cmd window (cmd /k keeps it open even on error)
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
bat = dir & "\pack.bat"
Set sh = CreateObject("WScript.Shell")
cmdLine = "cmd.exe /k call """ & bat & """"
sh.Run cmdLine, 1, False