Option Explicit

Dim shell, fs, root, python, server, outLog, errLog, command, quote
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")

root = fs.GetParentFolderName(WScript.ScriptFullName)
python = root & "\.venv\Scripts\python.exe"
server = root & "\object\server.py"
outLog = root & "\runtime\nota.stdout.log"
errLog = root & "\runtime\nota.stderr.log"
quote = Chr(34)

' cmd.exe owns the Python process; WScript starts it fully hidden.
command = "cmd.exe /d /c " & quote & quote & python & quote & " " & quote & _
          server & quote & " 1>>" & quote & outLog & quote & " 2>>" & quote & _
          errLog & quote & quote
shell.Run command, 0, False
