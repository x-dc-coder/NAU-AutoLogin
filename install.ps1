<#
.SYNOPSIS
    NAU-AutoLogin 南京审计大学校园网自动登录与双网卡保活一键部署与管理脚本 (免管理员权限)
.DESCRIPTION
    部署至 %USERPROFILE%\.nau-autologin\ 并在当前用户注册表添加开机静默自启动。
.PARAMETER Status
    查看守护进程运行状态与最新日志。
.PARAMETER Start
    启动后台守护进程。
.PARAMETER Stop
    停止后台守护进程。
.PARAMETER Restart
    重启后台守护进程。
.PARAMETER Uninstall
    卸载开机自启并终止守护进程（保留配置文件与日志）。
.PARAMETER Diagnose
    运行多维状态与 AP 优选自检工具。
#>

[CmdletBinding()]
param(
    [switch]$Status,
    [switch]$Start,
    [switch]$Stop,
    [switch]$Restart,
    [switch]$Uninstall,
    [switch]$Diagnose
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$AppName = "NAU-AutoLogin"
$DeployDir = Join-Path $env:USERPROFILE ".nau-autologin"
$TargetScript = Join-Path $DeployDir "nau_autologin.pyw"
$TargetConfig = Join-Path $DeployDir "config.ini"
$TargetEnv = Join-Path $DeployDir ".env"
$TargetDiagnose = Join-Path $DeployDir "diagnose.py"
$LogFile = Join-Path $DeployDir "autologin.log"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

function Get-PythonwPath {
    # 1. 尝试从当前正在运行的 python 中推导
    $pyCmd = Get-Command "python" -ErrorAction SilentlyContinue
    if ($pyCmd) {
        $pyDir = Split-Path (Split-Path $pyCmd.Source -Parent) -Parent
        $candidates = @(
            (Join-Path (Split-Path $pyCmd.Source -Parent) "pythonw.exe"),
            (Join-Path $pyDir "pythonw.exe")
        )
        foreach ($c in $candidates) {
            if (Test-Path $c) { return $c }
        }
    }
    
    # 2. 常见安装路径扫描
    $searchPaths = @(
        "D:\Anconda\pythonw.exe",
        "C:\ProgramData\anaconda3\pythonw.exe",
        "C:\ProgramData\miniconda3\pythonw.exe",
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\pythonw.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\pythonw.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\pythonw.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python39\pythonw.exe"),
        "C:\Python312\pythonw.exe",
        "C:\Python311\pythonw.exe"
    )
    foreach ($p in $searchPaths) {
        if (Test-Path $p) { return $p }
    }
    
    # 3. PATH 查找
    $pwCmd = Get-Command "pythonw" -ErrorAction SilentlyContinue
    if ($pwCmd) { return $pwCmd.Source }
    
    throw "未找到 pythonw.exe！请确认系统中已安装 Python 3.8+ 且包含 pythonw.exe。"
}

function Get-PythonCliPath {
    $pyCmd = Get-Command "python" -ErrorAction SilentlyContinue
    if ($pyCmd) { return $pyCmd.Source }
    $pw = Get-PythonwPath
    $py = $pw -replace "pythonw\.exe$", "python.exe"
    if (Test-Path $py) { return $py }
    return "python"
}

function Get-RunningProcess {
    $procs = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match "^python" -and $_.CommandLine -match "nau_autologin\.pyw"
    }
    return $procs
}

# ----------------- 操作分支 -----------------

if ($Diagnose) {
    $pyCli = Get-PythonCliPath
    $diagScript = if (Test-Path $TargetDiagnose) { $TargetDiagnose } else { Join-Path $PSScriptRoot "diagnose.py" }
    & $pyCli $diagScript
    exit 0
}

if ($Status) {
    Write-Host "===== [$AppName 运行状态] =====" -ForegroundColor Cyan
    $procs = Get-RunningProcess
    if ($procs) {
        foreach ($p in $procs) {
            Write-Host "守护进程正在运行 (PID: $($p.ProcessId))" -ForegroundColor Green
            Write-Host "命令行: $($p.CommandLine)" -ForegroundColor DarkGray
        }
    } else {
        Write-Host "守护进程未运行" -ForegroundColor Yellow
    }
    
    $runVal = (Get-ItemProperty -Path $RunKey -Name $AppName -ErrorAction SilentlyContinue).$AppName
    if ($runVal) {
        Write-Host "开机自启已启用: $runVal" -ForegroundColor Green
    } else {
        Write-Host "开机自启: 未配置" -ForegroundColor DarkGray
    }
    
    if (Test-Path $LogFile) {
        Write-Host "`n最新 20 行日志 ($LogFile):" -ForegroundColor Cyan
        Get-Content $LogFile -Tail 20 -Encoding UTF8
    } else {
        Write-Host "日志文件尚未生成 ($LogFile)" -ForegroundColor DarkGray
    }
    exit 0
}

if ($Stop) {
    Write-Host "正在停止 $AppName 守护进程..." -ForegroundColor Yellow
    $procs = Get-RunningProcess
    if ($procs) {
        foreach ($p in $procs) {
            Stop-Process -Id $p.ProcessId -Force
            Write-Host "已终止进程 PID: $($p.ProcessId)" -ForegroundColor Green
        }
    } else {
        Write-Host "当前无正在运行的守护进程。" -ForegroundColor Gray
    }
    exit 0
}

if ($Uninstall) {
    Write-Host "正在卸载 $AppName 自启动配置..." -ForegroundColor Yellow
    # 1. 停止进程
    $procs = Get-RunningProcess
    if ($procs) {
        foreach ($p in $procs) {
            Stop-Process -Id $p.ProcessId -Force
            Write-Host "已终止运行进程 PID: $($p.ProcessId)" -ForegroundColor Green
        }
    }
    # 2. 移除注册表
    Remove-ItemProperty -Path $RunKey -Name $AppName -ErrorAction SilentlyContinue
    Write-Host "已从注册表 Run 项中移除 $AppName" -ForegroundColor Green
    Write-Host "卸载完成！配置文件与日志保留于: $DeployDir" -ForegroundColor Cyan
    exit 0
}

# 部署 / 安装 / 启动 / 重启逻辑
$pythonw = Get-PythonwPath
Write-Host "检测到 pythonw 路径: $pythonw" -ForegroundColor Green

# 停止旧进程（若有）
$oldProcs = Get-RunningProcess
if ($oldProcs) {
    Write-Host "发现已有进程运行，正在安全停止..." -ForegroundColor Yellow
    foreach ($p in $oldProcs) {
        Stop-Process -Id $p.ProcessId -Force
    }
}
# 同时也停掉旧的 CampusPortalKeepalive / portal_keepalive_win 进程
$legacyProcs = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match "^python" -and $_.CommandLine -match "portal_keepalive_win\.pyw"
}
if ($legacyProcs) {
    Write-Host "正在清理旧版 win-portal-keepalive 进程..." -ForegroundColor Yellow
    foreach ($p in $legacyProcs) {
        Stop-Process -Id $p.ProcessId -Force
    }
    Remove-ItemProperty -Path $RunKey -Name "CampusPortalKeepalive" -ErrorAction SilentlyContinue
}

# 创建目标目录
if (-not (Test-Path $DeployDir)) {
    New-Item -ItemType Directory -Path $DeployDir -Force | Out-Null
}

# 同步文件
$sourceDir = $PSScriptRoot
Write-Host "正在同步文件至 $DeployDir ..." -ForegroundColor Cyan
Copy-Item (Join-Path $sourceDir "nau_autologin.pyw") $TargetScript -Force
Copy-Item (Join-Path $sourceDir "diagnose.py") $TargetDiagnose -Force

# 同步 config.ini
$srcConfig = Join-Path $sourceDir "config.ini"
if (Test-Path $srcConfig) {
    Copy-Item $srcConfig $TargetConfig -Force
}

# 同步 .env 敏感认证文件
$srcEnv = Join-Path $sourceDir ".env"
if (Test-Path $srcEnv) {
    Copy-Item $srcEnv $TargetEnv -Force
    Write-Host "已将 .env 认证环境变量同步至 $DeployDir" -ForegroundColor Green
} elseif (-not (Test-Path $TargetEnv)) {
    Write-Host "警告: 未找到 .env 文件！请从 .env.example 复制并配置密码。" -ForegroundColor Red
}

# 配置注册表自启
$runCmd = "`"$pythonw`" `"$TargetScript`""
Set-ItemProperty -Path $RunKey -Name $AppName -Value $runCmd
Write-Host "已配置开机静默自启动: $AppName" -ForegroundColor Green

# 启动后台守护进程 (无控制台窗口)
Write-Host "正在启动后台守护进程..." -ForegroundColor Cyan
$p = Start-Process -FilePath $pythonw -ArgumentList "`"$TargetScript`"" -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 2

# 复核状态
$running = Get-RunningProcess
if ($running) {
    Write-Host "`n部署成功！$AppName 已在后台静默运行 (PID: $($running[0].ProcessId))" -ForegroundColor Green
    Write-Host "日志文件: $LogFile" -ForegroundColor Cyan
    Write-Host "`n可用管理指令:"
    Write-Host "  查看状态与日志: powershell -File install.ps1 -Status"
    Write-Host "  运行全网诊断:   powershell -File install.ps1 -Diagnose"
    Write-Host "  停止守护进程:   powershell -File install.ps1 -Stop"
    Write-Host "  重启守护进程:   powershell -File install.ps1 -Restart"
    Write-Host "  卸载自启项:     powershell -File install.ps1 -Uninstall"
} else {
    Write-Host "启动后进程未检测到，请查看日志: $LogFile" -ForegroundColor Yellow
}
