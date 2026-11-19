<#
  stop_all.ps1 —— 只停「本副本」启动的服务（铺在 <副本根>\ops\deploy\）
  =============================================================================
  入口：副本根目录的 stop.bat（薄壳，只调本脚本）。

  为什么不能照抄生产 stop.bat：生产那套按端口/进程名分层 taskkill /T /F，
  在**同一台机器上试跑副本**时会把生产实例一起杀掉。这里改成：
    · 只认 start_all.ps1 写在 TMP\portable-<name>.pid 的 pid 文件；
    · 每个 pid 都必须同时通过「进程名 + 启动时间 ticks」校验才动手
      （pid 会被 Windows 复用，只比 pid 会误杀无关进程）；
    · 绝不按端口盲杀、绝不 tree kill、绝不碰计划任务 / watchdog。
  因此：在别的机器上没跑过 start_all.ps1 就调本脚本，结果应当是「无需停止」。

  nginx 特殊：优先用它自己的 -s quit 优雅退出（会读副本 logs\nginx.pid），
  pid 文件只作兜底。便携 PG 用 pg_ctl -m fast 停，只针对 <Root>\runtime\pgdata。
  P1-6 外部目录：设了 NOC_HOME 时，pid 文件、pgdata、nginx 配置/日志一律走
  外部目录（解析见 noc-paths.ps1，与 start_all.ps1 共用）；不设则与旧行为一致。
  =============================================================================
#>
[CmdletBinding()]
param([switch]$DryRun)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
# 2026-11-19 生产树守卫：本脚本会 `nginx -s quit`，而生产 nginx 正持有 80/443；
# 它跟踪的 TMP\portable-*.pid 在生产树上并不存在 —— 结果会是「把线上入口停了、
# 却没停掉后端」。所以生产树一律拒绝，让调用方走 portal-ctl.ps1。
if (Test-Path -LiteralPath (Join-Path $Root 'PRODUCTION.flag')) {
    throw @'
这是生产树（根目录有 PRODUCTION.flag），拒绝用隔离停止器停止。
生产停止走 ops\deploy\portal-ctl.ps1 -Action stop，或 ops\deploy\olbr-stop.bat（带 Y/N 确认），
或根目录 stop.bat（按标记分流）。
'@
}
# 与 start_all.ps1 同一约定：角色不明就不跑。丢 ISOLATED_COPY.flag 时本脚本
# 跟踪的 portable-*.pid 不属于这棵树，仍会执行步骤 1 的 `nginx -s quit`。
if (-not (Test-Path -LiteralPath (Join-Path $Root 'ISOLATED_COPY.flag'))) {
    throw '缺 ISOLATED_COPY.flag：本脚本只能在便携副本里运行，拒绝在主仓库停止'
}
# P1-6：目录解析集中在 noc-paths.ps1（与 start_all.ps1 共用，唯一真相源）。
$nocPathsLib = Join-Path $PSScriptRoot 'noc-paths.ps1'
if (-not (Test-Path -LiteralPath $nocPathsLib)) { throw "缺少目录解析脚本: $nocPathsLib（请从仓库重新取 ops\deploy\）" }
. $nocPathsLib
$paths = Get-NocPaths -Root $Root
$TmpDir = $paths.TmpDir
$RuntimeDir = $paths.RuntimeDir
$NginxDir = $paths.NginxDir
$NginxExe = Join-Path $NginxDir 'nginx.exe'
$PgData = $paths.PgData
$PgCtl = Join-Path $paths.PgBin 'pg_ctl.exe'

function Write-Step { param([string]$m) Write-Host "[*] $m" }
function Write-Ok { param([string]$m) Write-Host "[OK] $m" -ForegroundColor Green }
function Write-Warn2 { param([string]$m) Write-Host "[WARN] $m" -ForegroundColor Yellow }
function Write-Skip { param([string]$m) Write-Host "[SKIP] $m" -ForegroundColor DarkGray }

function Get-PidFile { param([string]$Name) Join-Path $TmpDir ("portable-$Name.pid") }

# 返回「可信 pid」或 0。三重校验：pid 存在 + 进程名一致 + 启动时间一致。
function Get-TrustedPid {
    param([string]$Name)
    $pf = Get-PidFile $Name
    if (-not (Test-Path -LiteralPath $pf)) { return 0 }
    $raw = ([System.IO.File]::ReadAllText($pf)).Trim()
    $parts = $raw -split '\|'
    if ($parts.Count -ne 3) { Write-Warn2 "$Name 的 pid 文件损坏，已跳过：$raw"; return 0 }
    $pidNum = 0
    $ticks = [long]0
    if (-not [int]::TryParse($parts[0], [ref]$pidNum) -or -not [long]::TryParse($parts[2], [ref]$ticks)) { return 0 }
    $p = Get-Process -Id $pidNum -ErrorAction SilentlyContinue
    if (-not $p) { return 0 }
    if ($p.ProcessName -ne $parts[1]) { return 0 }
    if ($p.StartTime.Ticks -ne $ticks) { return 0 }
    return $pidNum
}

function Stop-One {
    param([string]$Name)
    $pidNum = Get-TrustedPid $Name
    if ($pidNum -eq 0) {
        Write-Skip "$Name 无本副本的存活进程"
    } else {
        if ($DryRun) {
            Write-Step "<干跑> 将停止 $Name (pid=$pidNum)"
        } else {
            try {
                Stop-Process -Id $pidNum -Force -ErrorAction Stop
                Write-Ok "$Name 已停止 (pid=$pidNum)"
            } catch {
                Write-Warn2 "$Name (pid=$pidNum) 停止失败：$($_.Exception.Message)"
            }
        }
    }
    if (-not $DryRun) { Remove-Item -LiteralPath (Get-PidFile $Name) -Force -ErrorAction SilentlyContinue }
}

Write-Step "停止副本：$Root"
Write-Host ''

# ------------------------------------------------------ 1) nginx 优雅退出
Write-Step '--- 1/7 nginx ---'
$nginxPidFile = Join-Path $paths.NginxLogsDir 'nginx.pid'
if ((Test-Path -LiteralPath $NginxExe) -and (Test-Path -LiteralPath $nginxPidFile)) {
    # 先校验 nginx.pid 指向的进程确属副本（exe 在副本根下），再发 -s quit；
    # stale pid 可能被系统复用，盲发信号会误伤无关进程。
    $npid = ([System.IO.File]::ReadAllText($nginxPidFile)).Trim()
    $trusted = $false
    if ($npid -match '^\d+$') {
        $p = Get-Process -Id ([int]$npid) -ErrorAction SilentlyContinue
        if ($p) {
            $exe = try { $p.Path } catch { '' }
            if ($exe -and ($exe.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase) -or $exe.StartsWith($RuntimeDir, [StringComparison]::OrdinalIgnoreCase))) { $trusted = $true }
        }
    }
    if ($trusted) {
        if ($DryRun) { Write-Step '<干跑> 将执行 nginx -s quit' }
        else {
            # -c 绝对路径：外部模式配置在 config/ 下；便携模式与默认 conf 相同文件。
            $nginxConfArg = $paths.NginxConfOut.Replace('\', '/')
            & $NginxExe -s quit -p "$NginxDir\" -c "$nginxConfArg" 2>&1 | ForEach-Object { Write-Host "  $_" }
            Write-Ok 'nginx -s quit 已发出'
            Start-Sleep -Milliseconds 800
        }
    } else { Write-Skip 'nginx.pid 不可信（进程不存在或不属于副本），跳过 -s quit' }
} else { Write-Skip 'nginx 未运行（无 nginx.pid）' }
Stop-One 'nginx'

# ------------------------------------------------ 2~6) 应用进程，按启动逆序
foreach ($name in @('wa_bot', 'wa_analysis', 'portal', 'frontend', 'backend')) {
    Write-Step "--- $name ---"
    Stop-One $name
}

# --------------------------------------------------------- 7) 便携 PG
Write-Step '--- 7/7 便携 PostgreSQL ---'
if ((Test-Path -LiteralPath $PgCtl) -and (Test-Path -LiteralPath (Join-Path $PgData 'PG_VERSION'))) {
    if ($DryRun) { Write-Step '<干跑> 将执行 pg_ctl stop -m fast' }
    else {
        & $PgCtl -D $PgData status 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            & $PgCtl -D $PgData -m fast stop 2>&1 | ForEach-Object { Write-Host "  $_" }
            if ($LASTEXITCODE -eq 0) { Write-Ok '便携 PG 已停止' }
            else { Write-Warn2 "pg_ctl stop 返回 exit=$LASTEXITCODE" }
        } else { Write-Skip '便携 PG 未运行' }
    }
} else { Write-Skip '便携 PG 未初始化 / 缺 pg_ctl.exe' }

Write-Host ''
Write-Ok "完成：只处理了由 start_all.ps1 记录的本副本进程，未触碰任何生产实例。"
