<#
  portal-ctl.ps1 —— D:\02_olbr_portal 生产栈的启动 / 停止 / 状态
  ==========================================================================
  2026-11-18 起 02 就是生产（80/443/7038/7090/9267/9268，PG 用系统的 5432）。

  为什么 start 不直接调 start_hidden.ps1：
    三个「自愈任务」每 5 分钟跑的就是同一套逻辑（start_ensure.bat / start_hidden.ps1 /
    nginx_monitor.ps1），直接 `schtasks /run` 它们，等于**手动提前触发一次**——
    路径与定时任务完全一致，不会出现「手动能起、重启后起不来」的两套代码。
    （apps\my_app\start.bat 不能用于 02：它硬编码转发到 D:\01_olbr_portal。）

  用法：
    powershell -NoProfile -File ops\deploy\portal-ctl.ps1 -Action status
    powershell -NoProfile -File ops\deploy\portal-ctl.ps1 -Action start
    powershell -NoProfile -File ops\deploy\portal-ctl.ps1 -Action stop
    powershell -NoProfile -File ops\deploy\portal-ctl.ps1 -Action restart
  桌面快捷方式见同目录 olbr-start.bat / olbr-stop.bat / olbr-status.bat。
#>
[CmdletBinding()]
param(
    [ValidateSet('start', 'stop', 'restart', 'status')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
if ($Root -notlike '*02_olbr_portal') { throw "本脚本只用于 D:\02_olbr_portal（当前 $Root）" }
$NginxDir = Join-Path $Root 'nginx-1.28.0'
$Ports = @(80, 443, 7038, 7090, 9267, 9268)
$Tasks = @('MyApp_EnsureRunning', 'Portal_EnsureRunning', 'Nginx_EnsureRunning')

function Get-PortPid {
    param([int]$Port)
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return [int]$c.OwningProcess }
    return 0
}
function Get-PortOwner {
    param([int]$Port)
    $procId = Get-PortPid $Port
    if ($procId -eq 0) { return '' }
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
    if ($p) { return [string]$p.ExecutablePath }
    return ''
}
function Test-HttpOk {
    param([string]$Url)
    try {
        $r = Invoke-WebRequest -Uri $Url -TimeoutSec 8 -UseBasicParsing
        return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 400)
    } catch { return $false }
}

function Show-Status {
    Write-Host ''
    Write-Host '===== OLBR 生产栈状态 =====' -ForegroundColor Cyan
    Write-Host ("根目录：{0}" -f $Root) -ForegroundColor DarkGray
    # uvicorn 的命令行里没有项目路径（exe 是通用 pythonw.exe），归属靠 backend_watchdog 的 pid 反推
    $wdPid = 0
    $wdFile = Join-Path $Root 'apps\my_app\backend\backend_watchdog.pid'
    if (Test-Path -LiteralPath $wdFile) { $wdPid = [int]((Get-Content -LiteralPath $wdFile -Raw).Trim()) }
    foreach ($p in $Ports) {
        $procId = Get-PortPid $p
        if ($procId -eq 0) {
            Write-Host ("  {0,-5} 停" -f $p) -ForegroundColor Red
        } else {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
            $exe = if ($proc) { [string]$proc.ExecutablePath } else { '' }
            $cmd = if ($proc -and $proc.CommandLine) { [string]$proc.CommandLine } else { '' }
            $leaf = Split-Path $exe -Leaf
            # node/python 的 exe 路径不含项目名，归属要看命令行（-m uvicorn / tsx server / 02 路径）
            $tag = '?'
            if (($exe + ' ' + $cmd) -like '*02_olbr_portal*') { $tag = '02' }
            elseif (($exe + ' ' + $cmd) -like '*01_olbr_portal*' -or ($exe + ' ' + $cmd) -like '*01_RPA*') { $tag = '01' }
            elseif ($wdPid -gt 0 -and $proc -and $proc.ParentProcessId -eq $wdPid) { $tag = '02' }
            Write-Host ("  {0,-5} 跑着  pid={1,-6} {2} [{3}]" -f $p, $procId, $leaf, $tag) -ForegroundColor Green
        }
    }
    $h1 = Test-HttpOk 'http://127.0.0.1:7038/api/health'
    $h2 = Test-HttpOk 'http://127.0.0.1/myapp/'
    $h3 = Test-HttpOk 'http://127.0.0.1/uniplam/'
    Write-Host ("  健康：后端 {0} · /myapp/ {1} · /uniplam/ {2}" -f $(if ($h1) { 'OK' } else { 'FAIL' }), $(if ($h2) { 'OK' } else { 'FAIL' }), $(if ($h3) { 'OK' } else { 'FAIL' })) -ForegroundColor $(if ($h1 -and $h2 -and $h3) { 'Green' } else { 'Yellow' })
    foreach ($n in $Tasks) {
        $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
        if ($t) { Write-Host ("  任务 {0,-22} {1}" -f $n, $t.State) -ForegroundColor DarkGray }
    }
    Write-Host ''
}

function Wait-AllPorts {
    param([int]$Seconds)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $missing = @($Ports | Where-Object { (Get-PortPid $_) -eq 0 })
        if ($missing.Count -eq 0) { return @() }
        Start-Sleep -Seconds 3
    }
    return @($Ports | Where-Object { (Get-PortPid $_) -eq 0 })
}

function Wait-AllFree {
    param([int]$Seconds)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $busy = @($Ports | Where-Object { (Get-PortPid $_) -ne 0 })
        if ($busy.Count -eq 0) { return @() }
        Start-Sleep -Seconds 2
    }
    return @($Ports | Where-Object { (Get-PortPid $_) -ne 0 })
}

function Invoke-Start {
    Write-Host ''
    Write-Host '===== 启动（触发自愈任务，与 5 分钟周期同一路径）=====' -ForegroundColor Cyan
    # start.bat 的等价动作：清掉「人为停止」标记，否则 -Quiet 的 ensure 会跳过
    foreach ($f in (Join-Path $Root 'apps\my_app\backend\stopped.flag'), (Join-Path $Root 'apps\portal\stopped.flag')) {
        if (Test-Path -LiteralPath $f) { Remove-Item -LiteralPath $f -Force; Write-Host ("  清除 {0}" -f $f) -ForegroundColor DarkGray }
    }
    foreach ($n in $Tasks) {
        $out = & schtasks.exe /run /tn $n 2>&1
        Write-Host ("  触发 {0}：{1}" -f $n, ($out -join ' ')) -ForegroundColor DarkGray
    }
    Write-Host '  等待端口就绪（最多 180 秒）...' -ForegroundColor DarkGray
    $missing = Wait-AllPorts -Seconds 180
    if ($missing.Count -gt 0) {
        Write-Host ("[WARN] 这些端口还没起来：{0}" -f ($missing -join ', ')) -ForegroundColor Yellow
        Write-Host '       看日志：apps\my_app\log\start_hidden_result.txt / apps\portal\log\start_hidden_result.txt / TMP\nginx_monitor.log' -ForegroundColor Yellow
    } else {
        Write-Host '[OK] 全部端口已就绪' -ForegroundColor Green
    }
    Show-Status
}

function Invoke-Stop {
    Write-Host ''
    Write-Host '===== 停止（会写 stopped.flag，5 分钟自愈暂停，直到再次启动）=====' -ForegroundColor Cyan

    $stopMy = Join-Path $Root 'apps\my_app\stop.bat'
    if (Test-Path -LiteralPath $stopMy) {
        Write-Host '  停 my_app（backend/worker/frontend）...' -ForegroundColor DarkGray
        # 用 Start-Process -Wait 而不是管道：stop.bat 拉起的子进程会继承 stdout 句柄，
        # 一旦某个子进程没立刻退出，管道就读不到 EOF、整个脚本会假死。
        Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', $stopMy -Wait -NoNewWindow | Out-Null
    }
    $stopSvc = Join-Path $Root 'apps\portal\stop_services.ps1'
    if (Test-Path -LiteralPath $stopSvc) {
        Write-Host '  停门户 + WhatsApp 分析...' -ForegroundColor DarkGray
        Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $stopSvc, '-Quiet' -Wait -NoNewWindow | Out-Null
    }
    if (Test-Path -LiteralPath (Join-Path $NginxDir 'nginx.exe')) {
        Write-Host '  停 nginx...' -ForegroundColor DarkGray
        Push-Location $NginxDir
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'   # nginx 把提示写 stderr，Stop 会中断
        try { & .\nginx.exe -s quit 2>&1 | Out-Null } catch { } finally { $ErrorActionPreference = $prev; Pop-Location }
    }
    $busy = Wait-AllFree -Seconds 90
    if ($busy.Count -gt 0) {
        Write-Host ("[WARN] 这些端口仍被占用：{0}" -f ($busy -join ', ')) -ForegroundColor Yellow
    } else {
        Write-Host '[OK] 全部端口已释放' -ForegroundColor Green
    }
    Show-Status
}

switch ($Action) {
    'start' { Invoke-Start }
    'stop' { Invoke-Stop }
    'restart' { Invoke-Stop; Invoke-Start }
    default { Show-Status }
}
