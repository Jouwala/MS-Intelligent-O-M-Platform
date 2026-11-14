<#
  smoke-watcher.ps1 — D:\02 本机冒烟轮询器（替代 Grok Routine 的触发 + 回写）
  ============================================================================
  Grok 链路原来是：CI 变绿 → agent-orchestrator POST webhook → Grok Routine
  在 D:\02 上 detached checkout 到 PR head → 跑冒烟 → 贴评论、打
  smoke-passed → auto-merge 合入。这条脚本把中间两步搬到本机：每 60 秒
  轮询 GitHub，看到 CI 已绿、且没有 smoke-passed 的 open PR，**先切到该
  PR 的 head**，再跑 smoke-local.ps1，贴回评论、通过才打标签。0 token。

  ⛔ 生产边界（2026-11-14 用户明确要求）
  ----------------------------------------
  D:\01_olbr_portal 是生产，**不许动它里面任何东西**。本脚本只在
  D:\02_olbr_portal 隔离副本里工作；开头有硬守卫，一旦推导出的根目录
  落在 D:\01 下面就直接拒绝执行。冒烟链路对 D:\01 只有一处只读用途：
  看 80/443 的 nginx 归属、读它家 app.db 做三表比对，从不写入。

  触发条件（与 agent-orchestrator 的去重逻辑一致）：
    · PR open、非草稿、base 是 main；
    · 同一 head SHA 的 quality 工作流有一次 completed + success；
    · 该 PR 还没有 smoke-passed 标签，也没有 do-not-merge；
    · 同一 head SHA 没被本机冒烟过（状态文件 TMP\smoke-watcher-state.json）。

  为什么要 checkout（2026-11-14 实测补上）
  ----------------------------------------
  smoke-local.ps1 会校验「当前 HEAD == -HeadSha」，不等就 exit 2 并拒绝冒烟。
  Grok 当年是自己 checkout 的；watcher 若不 checkout，每个 PR 都只会拿到
  一条「HEAD 不匹配」的失败回帖，auto-merge 永远等不到门禁。所以这里做
  和 Grok 同样的事：
    1. 先记住当前分支与 HEAD；
    2. git fetch <remote> pull/<N>/head，校验取到的就是 PR head；
    3. git checkout --detach <sha>；
    4. 跑冒烟；
    5. **finally 里一定切回原分支/原提交**（冒烟失败、脚本异常都恢复）。
  安全阀：工作树有**未提交的已跟踪改动**时不动它，跳过该 PR 等下一轮，
  绝不把别人的在途改动 checkout 掉。未跟踪文件（TMP、构建产物）不影响。

  标签与 head SHA 的绑定：贴标签前重新读一次 PR，head 变了就放弃本次
  结果、不贴标签，等下一轮对新 head 重跑。绝不给旧 head 的结果贴到新
  head 上（phase-0 评估里的 P1 风险）。

  Token：优先环境变量 SMOKE_GITHUB_TOKEN，其次 GITHUB_TOKEN / GH_TOKEN；
  都没有时回落到本机已登录的 `gh auth token`（token 只在进程内存里，
  不写文件、不进日志）。全部拿不到时直接退出，不猜、不裸奔。

  日志：同时写 stdout 与 TMP\smoke-watcher.log（超过 5MB 轮转为 .1）。
  单实例：TMP\smoke-watcher.pid 守护，发现已有活的 watcher 就直接退出。

  用法（在 D:\02_olbr_portal 根目录）：
    powershell -NoProfile -File ops\deploy\smoke-watcher.ps1 -Once -DryRun
    powershell -NoProfile -File ops\deploy\smoke-watcher.ps1 -Once
    powershell -NoProfile -File ops\deploy\smoke-watcher.ps1 -OnlyPr 45 -Once
    powershell -NoProfile -File ops\deploy\smoke-watcher.ps1
  常驻：用 ops\deploy\install-smoke-watcher.ps1 注册计划任务
  （登录时自启 + 崩溃自动重启 + 已在跑就不再起第二个）。
#>
[CmdletBinding()]
param(
    [int]$PollSeconds = 60,
    [switch]$Once,
    [switch]$DryRun,
    [string]$Repo = 'Jouwala/noc-soc-intelligent-platform',
    [string]$Remote = 'platform',
    [int]$OnlyPr = 0
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$TmpDir = Join-Path $Root 'TMP'
$StateFile = Join-Path $TmpDir 'smoke-watcher-state.json'
$PidFile = Join-Path $TmpDir 'smoke-watcher.pid'
$LogFile = Join-Path $TmpDir 'smoke-watcher.log'
$SmokeScript = Join-Path $PSScriptRoot 'smoke-local.ps1'

# ⛔ 生产边界硬守卫：宁可不动，也不许任何路径落到 D:\01 下面。
$ProductionRoot = 'D:\01_olbr_portal'
if ($Root.TrimEnd('\') -eq $ProductionRoot -or $Root -like ($ProductionRoot + '\*')) {
    throw "拒绝执行：推导出的根目录是 $Root，落在生产目录 $ProductionRoot 下面。本链路只在 D:\02_olbr_portal 隔离副本上跑。"
}
if ($Root -notlike '*02_olbr_portal') { throw "当前目录不是 02（$Root），拒绝执行。" }
if (-not (Test-Path -LiteralPath (Join-Path $Root 'ISOLATED_COPY.flag'))) {
    throw '缺少 ISOLATED_COPY.flag，拒绝执行。'
}
if (-not (Test-Path -LiteralPath $SmokeScript)) { throw "找不到冒烟脚本：$SmokeScript" }

# ---------- token：环境变量优先，最后回落本机 gh 会话 ----------
$Token = $env:SMOKE_GITHUB_TOKEN
$TokenSource = 'SMOKE_GITHUB_TOKEN'
if (-not $Token) { $Token = $env:GITHUB_TOKEN; $TokenSource = 'GITHUB_TOKEN' }
if (-not $Token) { $Token = $env:GH_TOKEN; $TokenSource = 'GH_TOKEN' }
if (-not $Token) {
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $ghOut = (& gh auth token 2>$null | Select-Object -First 1)
        if ($ghOut -and ("$ghOut" -match '^gh[pousr]_')) {
            $Token = "$ghOut".Trim()
            $TokenSource = 'gh auth token'
        }
    } catch { } finally { $ErrorActionPreference = $prevEap }
}
if (-not $Token) { $TokenSource = 'none' }
if ((-not $Token) -and (-not $DryRun)) {
    throw '没有 GitHub token：请设置环境变量 SMOKE_GITHUB_TOKEN（细粒度 PAT，本仓库 Issues 读写即可），或先 gh auth login。'
}

function Write-WatchLog {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Host $line
    try {
        if (Test-Path -LiteralPath $LogFile) {
            if ((Get-Item -LiteralPath $LogFile).Length -gt 5MB) {
                Move-Item -LiteralPath $LogFile -Destination ($LogFile + '.1') -Force -ErrorAction SilentlyContinue
            }
        }
        Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
    } catch { }
}

# 只记一次：避免常驻时每 60 秒重复写同一条「还没绿」
$script:SkipLogged = @{}
function Write-SkipOnce {
    param([string]$Key, [string]$Message)
    if ($script:SkipLogged.ContainsKey($Key)) { return }
    $script:SkipLogged[$Key] = $true
    Write-WatchLog $Message
}

function Invoke-GitHubApi {
    param([string]$Path, [string]$Method = 'GET', [object]$Body = $null)
    $headers = @{
        'Accept' = 'application/vnd.github+json'
        'User-Agent' = 'smoke-watcher'
    }
    if ($Token) { $headers['Authorization'] = "Bearer $Token" }
    $uri = "https://api.github.com$Path"
    if ($null -eq $Body) {
        return Invoke-RestMethod -Uri $uri -Method $Method -Headers $headers -TimeoutSec 60
    }

    # PS 5.1 的两个坑（2026-11-14 本机贴评论返回 400/422 实测）：
    # 1) 把 Get-Content -Raw 读出的字符串塞进 hashtable，ConvertTo-Json 会序列化成
    #    {"body":{"value":"..."}} —— body 变成对象，GitHub 判非法。必须显式 [string]。
    # 2) -Body 传字符串时 PS 5.1 按 ANSI 编码发，中文变 '?' —— 统一发 UTF-8 字节。
    $normalized = @{}
    if ($Body -is [hashtable]) {
        foreach ($k in $Body.Keys) {
            $v = $Body[$k]
            if ($v -is [string]) { $normalized[$k] = [string]$v } else { $normalized[$k] = $v }
        }
    } else {
        $normalized = $Body
    }
    $json = ($normalized | ConvertTo-Json -Depth 5 -Compress)
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    try {
        return Invoke-RestMethod -Uri $uri -Method $Method -Headers $headers -Body $bytes -ContentType 'application/json; charset=utf-8' -TimeoutSec 60
    } catch {
        # 把 GitHub 返回的错误体带出来：否则日志里只有 (400) Bad Request，没法查。
        $detail = $_.Exception.Message
        try {
            $resp = $_.Exception.Response
            if ($resp) {
                $sr = New-Object IO.StreamReader($resp.GetResponseStream())
                $detail = $detail + ' | ' + $sr.ReadToEnd()
            }
        } catch { }
        throw ("GitHub API $Method $Path 失败：" + $detail)
    }
}

function Invoke-Git {
    param([string[]]$GitArgs)
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & git -C $Root @GitArgs 2>&1
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $prevEap }
    $text = (@($out) | ForEach-Object { "$_" }) -join "`n"
    return @{ Exit = $code; Out = $text.Trim() }
}

function Enter-PrCheckout {
    param([int]$PrNumber, [string]$Sha)
    $plan = @{ Ok = $false; Why = ''; OrigBranch = ''; OrigHead = '' }

    # 安全阀：只挡「已跟踪文件的未提交改动」。未跟踪/被忽略的文件（TMP、构建产物）
    # 不影响 checkout，也不该让 watcher 永远跳过。
    $st = Invoke-Git @('status', '--porcelain', '--untracked-files=no')
    if ($st.Exit -ne 0) { $plan.Why = "git status 失败：$($st.Out)"; return $plan }
    if ($st.Out) {
        $plan.Why = '工作树有未提交的已跟踪改动，不动它（避免覆盖他人在途工作）；等下一轮。改动：' + ($st.Out -replace "`n", ' | ')
        return $plan
    }

    $br = Invoke-Git @('rev-parse', '--abbrev-ref', 'HEAD')
    $hd = Invoke-Git @('rev-parse', 'HEAD')
    if ($br.Exit -ne 0 -or $hd.Exit -ne 0) { $plan.Why = '读不到当前 HEAD / 分支'; return $plan }
    $plan.OrigBranch = $br.Out
    $plan.OrigHead = $hd.Out

    $f = Invoke-Git @('fetch', '--no-tags', $Remote, ("pull/{0}/head" -f $PrNumber))
    if ($f.Exit -ne 0) { $plan.Why = "git fetch $Remote pull/$PrNumber/head 失败：$($f.Out)"; return $plan }
    $fetched = (Invoke-Git @('rev-parse', 'FETCH_HEAD')).Out
    if ($fetched -ne $Sha) {
        $plan.Why = "fetch 拿到 $fetched，与 PR head $Sha 不一致（可能刚推了新提交）；本轮放弃，下一轮重试"
        return $plan
    }

    $co = Invoke-Git @('checkout', '--detach', '--quiet', $Sha)
    if ($co.Exit -ne 0) { $plan.Why = "git checkout --detach 失败：$($co.Out)"; return $plan }
    $now = (Invoke-Git @('rev-parse', 'HEAD')).Out
    if ($now -ne $Sha) { $plan.Why = "checkout 之后 HEAD 是 $now，不是 $Sha"; return $plan }

    $plan.Ok = $true
    return $plan
}

function Exit-PrCheckout {
    param([hashtable]$Plan)
    if (-not $Plan -or -not $Plan.Ok) { return }
    $short = $Plan.OrigHead.Substring(0, 7)

    if ($Plan.OrigBranch -and $Plan.OrigBranch -ne 'HEAD') {
        $r = Invoke-Git @('checkout', '--quiet', $Plan.OrigBranch)
        if ($r.Exit -ne 0) { Write-WatchLog "恢复分支 $($Plan.OrigBranch) 失败：$($r.Out)，改按原提交恢复" }
    }
    $now = (Invoke-Git @('rev-parse', 'HEAD')).Out
    if ($now -eq $Plan.OrigHead) {
        Write-WatchLog "已恢复原分支 $($Plan.OrigBranch)（HEAD $short）"
        return
    }
    $null = Invoke-Git @('checkout', '--quiet', '--detach', $Plan.OrigHead)
    $now2 = (Invoke-Git @('rev-parse', 'HEAD')).Out
    if ($now2 -eq $Plan.OrigHead) {
        Write-WatchLog "已按原提交恢复（detached HEAD $short）"
    } else {
        Write-WatchLog "警告：恢复原 HEAD 失败，现在 $now2，应为 $($Plan.OrigHead)"
    }
}

function Enter-WatcherPidGuard {
    if (Test-Path -LiteralPath $PidFile) {
        $old = 0
        try { $old = [int]((Get-Content -LiteralPath $PidFile -Raw).Trim()) } catch { $old = 0 }
        if ($old -gt 0 -and $old -ne $PID) {
            $p = Get-CimInstance Win32_Process -Filter "ProcessId=$old" -ErrorAction SilentlyContinue
            if ($p -and ("$($p.CommandLine)" -match 'smoke-watcher\.ps1')) {
                throw "已有 smoke-watcher 在跑（pid=$old），本实例退出，避免两个轮询器同时冒烟。"
            }
        }
    }
    Set-Content -LiteralPath $PidFile -Value "$PID" -Encoding ASCII
}

function Exit-WatcherPidGuard {
    try {
        if (Test-Path -LiteralPath $PidFile) {
            $cur = (Get-Content -LiteralPath $PidFile -Raw).Trim()
            if ($cur -eq "$PID") { Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue }
        }
    } catch { }
}

function Get-State {
    # 状态一律用 hashtable（PS 5.1 可用），不用 PSCustomObject 的动态属性：
    # PR 号是纯数字键，"44" 这种属性名在 $obj.$key 写法下不可靠。
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

function Save-State {
    param([object]$State)
    New-Item -ItemType Directory -Force -Path $TmpDir | Out-Null
    ($State | ConvertTo-Json -Depth 5) | Out-File -LiteralPath $StateFile -Encoding UTF8
}

function Test-QualityGreen {
    param([string]$Sha)
    $runs = Invoke-GitHubApi "/repos/$Repo/actions/workflows/quality.yml/runs?head_sha=$Sha&per_page=5"
    foreach ($run in $runs.workflow_runs) {
        if ($run.status -eq 'completed' -and $run.conclusion -eq 'success') { return $true }
    }
    return $false
}

function Resolve-IssueNumber {
    param([object]$Pr)
    if ($Pr.body -match '(closes|fixes|resolves)\s+#(\d+)') { return [int]$Matches[2] }
    if ($Pr.body -match 'task:\s*#(\d+)') { return [int]$Matches[1] }
    if ($Pr.head.ref -match '^agent/task-(\d+)') { return [int]$Matches[1] }
    return 0
}

function Invoke-OneCycle {
    $state = Get-State

    if ($OnlyPr -gt 0) {
        $prs = @(Invoke-GitHubApi "/repos/$Repo/pulls/$OnlyPr")
        Write-WatchLog "只处理 PR #$OnlyPr（-OnlyPr，忽略本机已冒烟记录）"
    } else {
        $prs = Invoke-GitHubApi "/repos/$Repo/pulls?state=open&per_page=100"
    }

    foreach ($pr in $prs) {
        $n = $pr.number
        $sha = $pr.head.sha
        $short = $sha.Substring(0, 7)
        $labels = @($pr.labels | ForEach-Object { $_.name })

        if ($pr.draft) { continue }
        if ($pr.base.ref -ne 'main') { continue }
        if ($labels -contains 'smoke-passed') { continue }
        if ($labels -contains 'do-not-merge') { Write-SkipOnce "dnm-$n-$sha" "PR #$n 有 do-not-merge，跳过。"; continue }

        $key = "$n"
        if ($OnlyPr -le 0) {
            if ($state['smoked'][$key] -eq $sha) { continue }
            if ($state['failed'][$key] -eq $sha) {
                Write-SkipOnce "failed-$n-$sha" "PR #$n head $short 本机已冒烟失败过，等新 head 再跑。"
                continue
            }
        }

        Write-WatchLog "PR #$n head $short：查 CI…"
        try { $green = Test-QualityGreen $sha } catch {
            Write-WatchLog "PR #$n 查 CI 失败：$($_.Exception.Message)，下一轮重试。"
            continue
        }
        if (-not $green) {
            Write-SkipOnce "ci-$n-$sha" "PR #$n head $short CI 还没绿，等它绿。"
            continue
        }

        Write-WatchLog "PR #$n CI 已绿，开始冒烟（head $short）。"
        if ($DryRun) {
            Write-WatchLog "干跑：PR #$n 会 checkout $short 并跑冒烟，不实际执行、不贴评论、不打标签。"
            continue
        }

        $issue = Resolve-IssueNumber $pr

        # ---- 切到 PR head（Grok 当年做的事），跑冒烟，finally 一定切回 ----
        $plan = Enter-PrCheckout -PrNumber $n -Sha $sha
        if (-not $plan.Ok) {
            Write-SkipOnce "co-$n-$sha" "PR #$n 暂不冒烟：$($plan.Why)"
            continue
        }
        Write-WatchLog "已切到 PR #$n head $short（原分支 $($plan.OrigBranch) / $($plan.OrigHead.Substring(0,7))）"

        $smokeExit = 1
        try {
            $smokeArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $SmokeScript, '-PrNumber', "$n", '-HeadSha', $sha)
            if ($issue -gt 0) { $smokeArgs += @('-IssueNumber', "$issue") }
            Write-WatchLog ("执行：powershell " + ($smokeArgs -join ' '))
            & powershell.exe @smokeArgs
            $smokeExit = $LASTEXITCODE
            Write-WatchLog "smoke-local 退出码=$smokeExit"
        } finally {
            Exit-PrCheckout -Plan $plan
        }

        $passed = ($smokeExit -eq 0)
        $reportFile = Join-Path $TmpDir ("smoke-pr-{0}-{1}.md" -f $n, $short)
        $report = ''
        if (Test-Path -LiteralPath $reportFile) { $report = [string](Get-Content -LiteralPath $reportFile -Raw) }
        else { $report = "## D:\02 冒烟结果：失败`n`n冒烟脚本未生成报告（exit=$smokeExit），请看 TMP\smoke-pr-$n-$short.log。" }

        # 贴标签前重读 PR：head 变了就放弃本次结果，不贴标签。
        $fresh = Invoke-GitHubApi "/repos/$Repo/pulls/$n"
        if ($fresh.head.sha -ne $sha) {
            Write-WatchLog "PR #$n 冒烟期间 head 变了（$short -> $($fresh.head.sha.Substring(0,7))），本次结果作废、不贴标签，下一轮重跑。"
            continue
        }
        if ($report.Length -gt 60000) {
            $report = $report.Substring(0, 60000) + "`n`n（报告过长已截断，完整版在 D:\02 的 TMP\smoke-pr-$n-$short.md）"
        }
        Invoke-GitHubApi "/repos/$Repo/issues/$n/comments" 'POST' @{ body = $report } | Out-Null
        Write-WatchLog "PR #$n 冒烟评论已贴（通过=$passed）。"
        if ($passed) {
            $current = @((Invoke-GitHubApi "/repos/$Repo/issues/$n/labels") | ForEach-Object { $_.name })
            if ($current -notcontains 'smoke-passed') { $current += 'smoke-passed' }
            Invoke-GitHubApi "/repos/$Repo/issues/$n/labels" 'PUT' @{ labels = $current } | Out-Null
            $state['smoked'][$key] = $sha
            Write-WatchLog "PR #$n 已打 smoke-passed（head $short）。"
        } else {
            $state['failed'][$key] = $sha
            Write-WatchLog "PR #$n 冒烟未通过，未打标签；等新 head 再跑。"
        }
        Save-State $state
    }
}

Write-WatchLog "smoke-watcher 启动：repo=$Repo remote=$Remote 根=$Root 轮询=${PollSeconds}s Once=$Once DryRun=$DryRun OnlyPr=$OnlyPr token来源=$TokenSource"
Enter-WatcherPidGuard
try {
    if ($Once) {
        Invoke-OneCycle
        Write-WatchLog '单轮完成。'
    } else {
        while ($true) {
            try { Invoke-OneCycle } catch {
                Write-WatchLog "本轮出错：$($_.Exception.Message)，下一轮重试。"
            }
            Start-Sleep -Seconds $PollSeconds
        }
    }
} finally {
    Exit-WatcherPidGuard
}
