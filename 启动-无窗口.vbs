' Silent launcher (no console window) for the Material Capture + Classification Tool
' Double-click this file to start the app without a black console window.
' Keep it running in the background so scheduled plans can fire on time.
Option Explicit

Dim fso, shell, scriptDir, electronExe, appDir

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
electronExe = scriptDir & "\node_modules\electron\dist\electron.exe"
appDir = scriptDir

If Not fso.FileExists(electronExe) Then
    MsgBox "Electron not found:" & vbCrLf & electronExe & vbCrLf & vbCrLf & "Please run 'npm install' in this folder first.", vbCritical, "Launch failed"
    WScript.Quit 1
End If

If Not fso.FileExists(appDir & "\main.js") Then
    MsgBox "main.js not found in:" & vbCrLf & appDir, vbCritical, "Launch failed"
    WScript.Quit 1
End If

' 0 = hidden window, False = do not wait for exit
shell.Run """" & electronExe & """ """ & appDir & """", 0, False
