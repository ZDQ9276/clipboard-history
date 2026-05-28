@echo off
chcp 65001 >nul
title 历史粘接 - 一键安装

echo.
echo ============================================
echo    历史粘接 — 剪贴板历史管理器 安装程序
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请先安装 Python 3.10+
    echo 下载地址：https://www.python.org/downloads/
    echo （安装时请勾选 "Add Python to PATH"）
    pause
    exit /b 1
)
echo [√] Python 已安装
python --version

:: Install dependencies
echo.
echo [*] 正在安装依赖包...
pip install pillow pyperclip pystray keyboard -q
if %errorlevel% neq 0 (
    echo [错误] 依赖安装失败，请检查网络连接后重试
    pause
    exit /b 1
)
echo [√] 依赖包安装完成

:: Create desktop shortcut
echo.
echo [*] 正在创建桌面快捷方式...
powershell -ExecutionPolicy Bypass -Command ^
"$WshShell = New-Object -ComObject WScript.Shell; ^
$Desktop = [Environment]::GetFolderPath('Desktop'); ^
$Shortcut = $WshShell.CreateShortcut(\"$Desktop\历史粘接.lnk\"); ^
$Shortcut.TargetPath = (Get-Command pythonw).Source; ^
$Shortcut.Arguments = '\"%~dp0app.py\"'; ^
$Shortcut.WorkingDirectory = '%~dp0'; ^
$Shortcut.IconLocation = '%~dp0icon.ico'; ^
$Shortcut.Description = '历史粘接 - 剪贴板历史管理器'; ^
$Shortcut.Save()"
if %errorlevel% equ 0 (
    echo [√] 桌面快捷方式已创建
) else (
    echo [!] 快捷方式创建失败，可手动运行 app.py
)

:: Done
echo.
echo ============================================
echo    安装完成！
echo.
echo    按 Ctrl+Shift+V 呼出窗口
echo    桌面快捷方式"历史粘接"可直接双击启动
echo ============================================
echo.
pause
