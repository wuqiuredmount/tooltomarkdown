$ErrorActionPreference = "SilentlyContinue"

# 引入 Windows 原生 API
$api = @"
using System;
using System.Runtime.InteropServices;

public class NativeMethods {
    [DllImport("ntdll.dll")]
    public static extern uint NtSuspendProcess(IntPtr processHandle);

    [DllImport("ntdll.dll")]
    public static extern uint NtResumeProcess(IntPtr processHandle);

    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern IntPtr OpenProcess(uint processAccess, bool bInheritHandle, int processId);

    [DllImport("kernel32.dll", SetLastError=true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool CloseHandle(IntPtr hObject);
}
"@
Add-Type -TypeDefinition $api -ErrorAction SilentlyContinue

# 查找进程
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'"
$targets = $procs | Where-Object { $_.CommandLine -match 'ocr_all_batch.py' -or $_.CommandLine -match 'ocr_scanned.py' }

$stateFile = Join-Path $PSScriptRoot ".ocr_paused_state"

if (-not $targets) {
    Write-Host "`n========================================" -ForegroundColor DarkGray
    Write-Host "  🔴 状态：未运行（没有找到 OCR 进程）" -ForegroundColor Red
    Write-Host "========================================`n" -ForegroundColor DarkGray
    exit
}

$isPaused = Test-Path $stateFile

if (-not $isPaused) {
    Write-Host "`n========================================" -ForegroundColor DarkGray
    Write-Host "  🟢 当前状态：运行中" -ForegroundColor Green
    Write-Host "  正在执行：暂停 (挂起进程)..." -ForegroundColor Yellow
    Write-Host "========================================`n" -ForegroundColor DarkGray
    
    foreach ($t in $targets) {
        $h = [NativeMethods]::OpenProcess(0x0800, $false, $t.ProcessId)
        if ($h -ne [IntPtr]::Zero) {
            [NativeMethods]::NtSuspendProcess($h) | Out-Null
            [NativeMethods]::CloseHandle($h) | Out-Null
        }
    }
    New-Item -ItemType File -Path $stateFile | Out-Null
    Write-Host "  ⏸️ 切换成功：后台任务已全面冻结（不占用 CPU）。`n" -ForegroundColor Cyan
} else {
    Write-Host "`n========================================" -ForegroundColor DarkGray
    Write-Host "  🟡 当前状态：已暂停" -ForegroundColor Yellow
    Write-Host "  正在执行：恢复 (解冻进程)..." -ForegroundColor Green
    Write-Host "========================================`n" -ForegroundColor DarkGray
    
    foreach ($t in $targets) {
        $h = [NativeMethods]::OpenProcess(0x0800, $false, $t.ProcessId)
        if ($h -ne [IntPtr]::Zero) {
            [NativeMethods]::NtResumeProcess($h) | Out-Null
            [NativeMethods]::CloseHandle($h) | Out-Null
        }
    }
    Remove-Item $stateFile -ErrorAction SilentlyContinue
    Write-Host "  ▶️ 切换成功：后台任务已恢复运行。`n" -ForegroundColor Cyan
}
