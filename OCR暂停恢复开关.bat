@echo off
chcp 65001 >nul
title OCR 后台任务控制开关
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ocr_switch.ps1"
echo.
pause
