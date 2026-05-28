$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("$env:USERPROFILE\Desktop\历史粘接.lnk")
$Shortcut.TargetPath = "C:\Users\Administrator\AppData\Local\Programs\Python\Python314\pythonw.exe"
$Shortcut.Arguments = '"C:\Users\Administrator\Desktop\vsc\历史粘接\app.py"'
$Shortcut.WorkingDirectory = "C:\Users\Administrator\Desktop\vsc\历史粘接"
$Shortcut.IconLocation = "C:\Users\Administrator\Desktop\vsc\历史粘接\icon.ico"
$Shortcut.Description = "历史粘接 - 剪贴板历史管理器"
$Shortcut.WindowStyle = 7
$Shortcut.Save()
Write-Host "Shortcut created!"
