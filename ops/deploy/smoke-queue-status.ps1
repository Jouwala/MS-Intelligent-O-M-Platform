<#
  smoke-queue-status.ps1 — 只读：哪些 PR 在等冒烟 / 被挡 / 已完成
  =================================================================
  给 Muse 与值班人快速看队列，不抢 TMP\smoke-watcher.pid、不跑冒烟。
  默认仓库与 watcher 一致：Jouwala/noc-soc-intelligent-platform（remote=platform）。

  用法（D:\02_olbr_portal 根）：
    powershell -NoProfile -File ops\deploy\smoke-queue-status.ps1
    powershell -NoProfile -File ops\deploy\smoke-queue-status.ps1 -Json
#>
[CmdletBinding()]
param(
    [string]$Repo = 'Jouwala/noc-soc-intelligent-platform',
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$TmpDir = Join-Path $Root 'TMP'
$StateFile = Join-Path $TmpDir 'smoke-watcher-state.json'
$PidFile = Join-Path $TmpDir 'smoke-watcher.pid'
$LogFile = Join-Path $TmpDir 'smoke-watcher.log'
$LockPath = Join-Path $TmpDir 'smoke.lock'

if ($Root -notlike '*02_olbr_portal') { throw "当前目录不是 02（$Root），拒绝执行。" }

$Token = $env:SMOKE_GITHUB_TOKEN
if (-not $Token) { $Token = $env:GITHUB_TOKEN }
if (-not $Token) { $Token = $env:GH_TOKEN }
if (-not $Token) {
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
  try {
    $ghOut = (& gh auth token 2>$null | Select-Object -First 1)
    if ($ghOut -and ("$ghOut" -match '^gh[pousr]_')) { $Token = "$ghOut".Trim() }
  } catch { } finally { $ErrorActionPreference = $prevEap }
}
if (-not $Token) { throw '没有 GitHub token：请 gh auth login 或设置 SMOKE_GITHUB_TOKEN。' }

function Invoke-GitHubApi {
    param([string]$Path)
    $headers = @{
        'Accept' = 'application/vnd.github+json'
        'Authorization' = "Bearer $Token"
        'User-Agent' = 'smoke-queue-status'
    }
    return Invoke-RestMethod -Uri "https://api.github.com$Path" -Headers $headers -TimeoutSec 60
}

function Test-QualityGreen {
    param([string]$Sha)
    $runs = Invoke-GitHubApi "/repos/$Repo/actions/workflows/quality.yml/runs?head_sha=$Sha&per_page=5"
    foreach ($run in $runs.workflow_runs) {
        if ($run.status -eq 'completed' -and $run.conclusion -eq 'success') { return $true }
    }
    return $false
}

function Get-State {
    $state = @{ smoked = @{}; failed = @{} }
    if (Test-Path -LiteralPath $StateFile) {
        try {
            $raw = (Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json)
            foreach ($prop in @('smoked', 'failed')) {
                if ($null -ne $raw.$prop) {
                    foreach ($p in $raw.$prop.PSObject.Properties) { $state[$prop][$p.Name] = $p.Value }
                }
            }
        } catch { }
    }
    return $state
}

function Get-WatcherAlive {
    if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
    $old = 0
    try { $old = [int]((Get-Content -LiteralPath $PidFile -Raw).Trim()) } catch { return $null }
    if ($old -le 0) { return $null }
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$old" -ErrorAction SilentlyContinue
    if ($p -and ("$($p.CommandLine)" -match 'smoke-watcher\.ps1')) { return $old }
    return $null
}

$state = Get-State
$dirty = $false
$dirtyLines = @()
try {
    $st = & git -C $Root status --porcelain --untracked-files=no 2>&1
    if ($LASTEXITCODE -eq 0 -and "$st".Trim()) {
        $dirty = $true
        $dirtyLines = @($st | ForEach-Object { "$_" } | ForEach-Object { $_ -split "`r?`n" } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    }
} catch { }

$watcherPid = Get-WatcherAlive
$lockHeld = Test-Path -LiteralPath $LockPath
$lastLog = ''
if (Test-Path -LiteralPath $LogFile) {
    $lastLog = (Get-Content -LiteralPath $LogFile -Tail 1 -ErrorAction SilentlyContinue) -join ''
}

# 仅 1 条 open PR 时 REST 反序列化成单个对象；不强制 @() 时 foreach 会枚举属性而非 PR。
$prs = @(Invoke-GitHubApi "/repos/$Repo/pulls?state=open&per_page=100")
$rows = New-Object System.Collections.ArrayList

foreach ($pr in $prs) {
    if ($pr.draft -or $pr.base.ref -ne 'main') { continue }
    $n = $pr.number
    $sha = $pr.head.sha
    $short = $sha.Substring(0, 7)
    $labels = @($pr.labels | ForEach-Object { $_.name })

    $status = 'pending'
    $reason = ''

    if ($labels -contains 'do-not-merge') {
        $status = 'blocked'
        $reason = 'do-not-merge'
    } elseif ($labels -contains 'smoke-passed' -or $state.smoked["$n"] -eq $sha) {
        $status = 'done'
        $reason = 'smoke-passed'
    } elseif ($state.failed["$n"] -eq $sha) {
        $status = 'failed-local'
        $reason = '本机 state 已记失败，需新 head 或清 state'
    } elseif ($dirty) {
        $status = 'blocked'
        $reason = '工作树有已跟踪未提交改动，watcher 会跳过 checkout'
    } else {
        try {
            if (-not (Test-QualityGreen $sha)) {
                $status = 'wait-ci'
                $reason = 'quality 未绿'
            } else {
                $status = 'ready'
                $reason = 'CI 已绿，待 watcher 或手工 smoke-local'
            }
        } catch {
            $status = 'unknown'
            $reason = $_.Exception.Message
        }
    }

    [void]$rows.Add([ordered]@{
        pr          = $n
        head        = $short
        title       = $pr.title
        status      = $status
        reason      = $reason
        labels      = ($labels -join ', ')
    })
}

$infra = [ordered]@{
    repo        = $Repo
    root        = $Root
    watcherPid  = $watcherPid
    smokeLock   = $lockHeld
    dirtyTree   = $dirty
    dirtyFiles  = $dirtyLines
    lastLogLine = $lastLog
}

if ($Json) {
    ($infra + @{ pulls = @($rows) } | ConvertTo-Json -Depth 5)
    exit 0
}

Write-Host "=== 冒烟队列 ($Repo) ==="
Write-Host ("watcher: " + $(if ($watcherPid) { "运行中 pid=$watcherPid" } else { '未检测到（计划任务 OLBR_SmokeWatcher？）' }))
Write-Host ("smoke.lock: " + $(if ($lockHeld) { '存在（可能正在冒烟）' } else { '无' }))
Write-Host ("工作树: " + $(if ($dirty) { '有已跟踪未提交改动 — 会挡 checkout' } else { '干净' }))
if ($dirty -and $dirtyLines.Count) {
    Write-Host '  改动文件：'
    foreach ($ln in $dirtyLines | Select-Object -First 12) { Write-Host "    $ln" }
    if ($dirtyLines.Count -gt 12) { Write-Host ("    …另有 $($dirtyLines.Count - 12) 个") }
}
if ($lastLog) { Write-Host "日志末行: $lastLog" }
Write-Host ''

$ready = @($rows | Where-Object { $_.status -eq 'ready' })
$wait = @($rows | Where-Object { $_.status -eq 'wait-ci' })
$blocked = @($rows | Where-Object { $_.status -eq 'blocked' })
$failedLocal = @($rows | Where-Object { $_.status -eq 'failed-local' })
$done = @($rows | Where-Object { $_.status -eq 'done' })

function Show-Group {
    param([string]$Title, [array]$Items)
    if (-not $Items.Count) { return }
    Write-Host "--- $Title ($($Items.Count)) ---"
    foreach ($i in $Items) {
        Write-Host ("  PR #{0} {1} [{2}] {3}" -f $i.pr, $i.head, $i.status, $i.reason)
        if ($i.title.Length -gt 72) { Write-Host ("    {0}…" -f $i.title.Substring(0, 72)) }
        else { Write-Host "    $($i.title)" }
    }
    Write-Host ''
}

Show-Group '待冒烟（CI 已绿）' $ready
Show-Group '等 CI' $wait
Show-Group '跳过 / 阻塞' $blocked
Show-Group '本机已记失败（需新 head 或 -OnlyPr 重跑）' $failedLocal
Show-Group '已通过（本视角）' $done
if (-not $rows.Count) { Write-Host '（无符合 base=main 的非草稿 open PR）' }

if ($ready.Count -gt 0) {
    Write-Host '手工补跑（示例）：'
    $r0 = $ready[0]
    $full = ($prs | Where-Object { $_.number -eq $r0.pr }).head.sha
    Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File ops\deploy\smoke-local.ps1 -PrNumber $($r0.pr) -HeadSha $full"
    exit 2
}
exit 0