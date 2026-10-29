@echo off
chcp 65001 >nul
title Stop Console

echo Stopping console backend...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'review_ui.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo Stopped.
ping 127.0.0.1 -n 3 >nul
