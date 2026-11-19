<#
.SYNOPSIS
    生产检查点（切换后 / 每晨）：一条命令核完端口、接口、看板、任务、WhatsApp 链路。

.DESCRIPTION
    把 2026-11-19 切换后那次人工复核的检查项固化成脚本，默认“不定时、手动跑”。
    只读为主：唯一的写操作是触发原生看板预热（-NoPrewarm 可关）与在 TMP 下落一份 JSON 报告。
    判定口径来自 docs\workspace\cutover-2026-11-20.md 第七节与当次复核记录。

    检查范围：
      1. 端口与进程归属（80/443 必须是本树的 nginx，进程启动时间用于判断夜里是否重启过）
      2. HTTP 探活：7038 health(db_mode=pg)、7090、9267/9268、80/443 的 /myapp/ 与 /uniplam/
      3. 代码身份：openapi 路由数 + cn-hourly-kpi / ran-inventory 两个特征路由
      4. 任务与调度：近 N 小时运行数 / 失败数 / 失败按任务分组 / 调度器是否还在跑
      5. 17 个原生看板：预热后逐页核对 failed / stale / 组件 error / 数据里最新日期
      6. 关键看板数据新鲜度：CXO、CN KPI、OLIB SOC、可用率、MW、丢包、电池、TT&WO、SOC、MS SLA
      7. WhatsApp：bot 与 notify 心跳、pid、4 个计划任务是否仍指向本树
      8. 外部依赖：EWS(exchmail:443) 可达性（只报告，不判失败）
    可选 -Render：用 Playwright 无头渲染若干看板页，抓 console error 与失败接口。

.PARAMETER NoPrewarm
    不触发原生看板预热（只看现有缓存；缓存过期会报 WARN 而不是等预热）。

.PARAMETER Render
    额外做真实渲染检查（需要 python + playwright，缺依赖自动 SKIP）。

.PARAMETER SinceHours
    任务历史回看窗口，默认 12 小时（覆盖“过夜”）。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File ops\deploy\post-cutover-check.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File ops\deploy\post-cutover-check.ps1 -Render
#>
param(
    [switch]$NoPrewarm,
    [switch]$Render,
    [int]$SinceHours = 12
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Api = 'http://127.0.0.1:7038'
$results = New-Object System.Collections.ArrayList

function Add-Result {
    param([string]$Area, [string]$Name, [string]$Status, [string]$Detail = '')
    [void]$results.Add([pscustomobject]@{ Area = $Area; Name = $Name; Status = $Status; Detail = $Detail })
    $mark = switch ($Status) { 'PASS' { '[ OK ]' } 'WARN' { '[WARN]' } 'SKIP' { '[SKIP]' } default { '[FAIL]' } }
    $line = '{0} {1,-30} {2}' -f $mark, $Name, $Detail
    switch ($Status) {
        'FAIL' { Write-Host $line -ForegroundColor Red }
        'WARN' { Write-Host $line -ForegroundColor Yellow }
        default { Write-Host $line }
    }
}

function Write-Section([string]$Title) {
    Write-Host ''
    Write-Host ('── ' + $Title + ' ' + ('─' * [Math]::Max(0, 62 - $Title.Length))) -ForegroundColor Cyan
}

function Get-Text([string]$Url) {
    $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 60
    return [Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray())
}

function Get-Json([string]$Url) { return (Get-Text $Url) | ConvertFrom-Json }

function Get-Code([string]$Url) {
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 20 -MaximumRedirection 5
        return [int]$r.StatusCode
    } catch {
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
        return 0
    }
}

# 本机自签证书：PS 5.1 没有 -SkipCertificateCheck，统一用 curl 探 HTTPS
function Get-CodeHttps([string]$Url) {
    if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) { return -1 }
    $code = & curl.exe -k -s -o NUL -w '%{http_code}' --max-time 20 $Url 2>$null
    return [int]$code
}

function Test-Tcp([string]$HostName, [int]$Port, [int]$TimeoutMs = 5000) {
    $c = New-Object Net.Sockets.TcpClient
    try { return $c.ConnectAsync($HostName, $Port).Wait($TimeoutMs) } catch { return $false } finally { $c.Close() }
}

function Get-Listeners([int[]]$Ports) {
    $map = @{}
    try {
        Get-NetTCPConnection -State Listen -ErrorAction Stop |
            Where-Object { $Ports -contains $_.LocalPort } |
            ForEach-Object { if (-not $map.ContainsKey([int]$_.LocalPort)) { $map[[int]$_.LocalPort] = [int]$_.OwningProcess } }
    } catch {
        netstat -ano | Select-String 'LISTENING' | ForEach-Object {
            if ($_.Line -match ':(\d+)\s+\S+\s+LISTENING\s+(\d+)') {
                $p = [int]$matches[1]
                if (($Ports -contains $p) -and (-not $map.ContainsKey($p))) { $map[$p] = [int]$matches[2] }
            }
        }
    }
    return $map
}

function Get-ProcInfo([int]$ProcId) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcId" -ErrorAction SilentlyContinue
    if (-not $p) { return $null }
    return [pscustomobject]@{ Id = $ProcId; Path = $p.ExecutablePath; Started = $p.CreationDate }
}

$startedAt = Get-Date
Write-Host ''
Write-Host '══ 生产检查点 ══' -ForegroundColor Cyan
Write-Host ('   主机 ' + $env:COMPUTERNAME + '　根目录 ' + $Root + '　时间 ' + $startedAt.ToString('yyyy-MM-dd HH:mm:ss'))
Write-Host ('   回看窗口 ' + $SinceHours + ' 小时' + $(if ($NoPrewarm) { '　（不预热）' } else { '' }))

# ═══════════════════════════════════════════════════════════ 1. 端口与归属
Write-Section '1. 端口与进程归属'
$wantPorts = @(80, 443, 5432, 7038, 7090, 9267, 9268, 9269)
$listeners = Get-Listeners -Ports $wantPorts
foreach ($port in $wantPorts) {
    if ($listeners.ContainsKey($port)) {
        $proc = Get-ProcInfo -ProcId $listeners[$port]
        $path = if ($proc -and $proc.Path) { $proc.Path } else { '?' }
        $up = if ($proc -and $proc.Started) { [Math]::Round(((Get-Date) - $proc.Started).TotalHours, 1) } else { -1 }
        Add-Result '端口' ("listen $port") 'PASS' ("pid=" + $listeners[$port] + " up=" + $up + "h " + (Split-Path -Leaf $path))
        if ($port -eq 80 -or $port -eq 443) {
            if ($path -and $path.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)) {
                Add-Result '端口' ("$port 归属") 'PASS' $path
            } else {
                Add-Result '端口' ("$port 归属") 'FAIL' ("期望本树 nginx，实际 " + $path)
            }
        }
    } else {
        Add-Result '端口' ("listen $port") 'FAIL' '未监听'
    }
}

# ═══════════════════════════════════════════════════════════ 2. HTTP 探活
Write-Section '2. HTTP 探活'
try {
    $h = Get-Json "$Api/api/health"
    $detail = "status=$($h.status) db_mode=$($h.db_mode) db=$($h.database)"
    if ($h.status -eq 'ok' -and $h.db_mode -eq 'pg') { Add-Result '接口' '7038 /api/health' 'PASS' $detail }
    else { Add-Result '接口' '7038 /api/health' 'FAIL' $detail }
} catch { Add-Result '接口' '7038 /api/health' 'FAIL' $_.Exception.Message }

$httpChecks = @(
    @{ Name = '7090 /myapp/'; Url = 'http://127.0.0.1:7090/myapp/' },
    @{ Name = '9267 /uniplam/'; Url = 'http://127.0.0.1:9267/uniplam/' },
    @{ Name = '9268 /api/health'; Url = 'http://127.0.0.1:9268/api/health' },
    @{ Name = '80 /myapp/'; Url = 'http://127.0.0.1/myapp/' },
    @{ Name = '80 /uniplam/'; Url = 'http://127.0.0.1/uniplam/' },
    @{ Name = '80 /api/health'; Url = 'http://127.0.0.1/api/health' }
)
foreach ($c in $httpChecks) {
    $code = Get-Code $c.Url
    if ($code -eq 200) { Add-Result '接口' $c.Name 'PASS' '200' }
    else { Add-Result '接口' $c.Name 'FAIL' ("HTTP " + $code) }
}
foreach ($u in @('https://127.0.0.1/myapp/', 'https://127.0.0.1/uniplam/', 'https://127.0.0.1/api/health')) {
    $code = Get-CodeHttps $u
    if ($code -eq 200) { Add-Result '接口' ($u -replace 'https://127.0.0.1', '443') 'PASS' '200' }
    elseif ($code -eq -1) { Add-Result '接口' ($u -replace 'https://127.0.0.1', '443') 'SKIP' 'curl 不可用' }
    else { Add-Result '接口' ($u -replace 'https://127.0.0.1', '443') 'FAIL' ("HTTP " + $code) }
}

# ═══════════════════════════════════════════════════════════ 3. 代码身份
Write-Section '3. 代码身份（跑的是不是本树）'
try {
    $spec = Get-Json "$Api/openapi.json"
    $routes = @($spec.paths.PSObject.Properties.Name)
    $hourly = @($routes | Where-Object { $_ -like '*cn-hourly-kpi*' }).Count
    $raninv = @($routes | Where-Object { $_ -like '*ran-inventory*' }).Count
    $detail = "routes=$($routes.Count) cn-hourly-kpi=$hourly ran-inventory=$raninv"
    if ($hourly -gt 0 -and $raninv -gt 0) { Add-Result '身份' 'openapi 特征路由' 'PASS' $detail }
    else { Add-Result '身份' 'openapi 特征路由' 'FAIL' ($detail + '（疑似旧代码）') }
} catch { Add-Result '身份' 'openapi' 'FAIL' $_.Exception.Message }

# ═══════════════════════════════════════════════════════════ 4. 任务与调度
Write-Section '4. 任务与调度'
try {
    $hist = (Get-Json "$Api/api/tasks/history?limit=3000").history
    $since = (Get-Date).AddHours(-1 * $SinceHours)
    $win = @($hist | Where-Object { $_.started_at -and ([datetime]$_.started_at) -ge $since })
    $fails = @($win | Where-Object { -not $_.success })
    $rate = if ($win.Count) { [Math]::Round(100.0 * $fails.Count / $win.Count, 1) } else { 0 }
    $detail = "runs=$($win.Count) fail=$($fails.Count) ($rate%)"
    if ($win.Count -eq 0) { Add-Result '任务' '历史窗口内无运行' 'FAIL' '调度器可能没在跑' }
    elseif ($rate -le 5) { Add-Result '任务' "近 $SinceHours 小时运行" 'PASS' $detail }
    else { Add-Result '任务' "近 $SinceHours 小时运行" 'WARN' $detail }

    $recent = @($win | Where-Object { ([datetime]$_.started_at) -ge (Get-Date).AddMinutes(-20) })
    if ($recent.Count -gt 0) { Add-Result '任务' '调度器活跃' 'PASS' ("近 20 分钟 " + $recent.Count + " 次") }
    else { Add-Result '任务' '调度器活跃' 'WARN' '近 20 分钟没有任务运行' }

    if ($fails.Count -gt 0) {
        $fails | Group-Object task_id | Sort-Object Count -Descending | Select-Object -First 5 | ForEach-Object {
            $last = $_.Group | Sort-Object started_at -Descending | Select-Object -First 1
            $msg = ([string]$last.error -replace '\s+', ' ')
            if ($msg.Length -gt 90) { $msg = $msg.Substring(0, 90) }
            Add-Result '任务' ("失败×" + $_.Count + " " + $_.Name) 'WARN' $msg
        }
    }
} catch { Add-Result '任务' '任务历史' 'FAIL' $_.Exception.Message }

# ═══════════════════════════════════════════════════════════ 5. 原生看板
Write-Section '5. 原生看板（17 个）'
try {
    $boards = (Get-Json "$Api/api/native-boards").boards
    if (-not $NoPrewarm) {
        $pw = (Invoke-WebRequest -Uri "$Api/api/native-boards/_cache/prewarm" -Method POST -UseBasicParsing -TimeoutSec 60)
        $pending = @((([Text.Encoding]::UTF8.GetString($pw.RawContentStream.ToArray())) | ConvertFrom-Json).pending)
        # 预热是串行 + 每页间隔 2s（dashboard_service.prewarm）；全部命中时 pending 为空，直接跳过。
        # 有待办时按缓存文件年龄判断进度（比逐页拉整份 payload 轻得多）。
        if ($pending.Count -gt 0) {
            $deadline = (Get-Date).AddSeconds(180)
            while ((Get-Date) -lt $deadline) {
                Start-Sleep -Seconds 5
                $st = Get-Json "$Api/api/native-boards/_cache/stats"
                $ages = @($st.disk |
                    Where-Object { $pending -contains (($_.file -split '__')[0]) } |
                    ForEach-Object { [double]$_.age_s })
                if ($ages.Count -eq 0) { break }
                if (($ages | Measure-Object -Maximum).Maximum -lt 120) { break }
            }
        }
    }
    $bad = 0
    foreach ($b in $boards) {
        $d = Get-Json "$Api/api/native-boards/$($b.slug)"
        $errs = @($d.widgets | Where-Object { $_.error }).Count
        $dates = [regex]::Matches(($d | ConvertTo-Json -Depth 6 -Compress), '20\d\d-\d\d-\d\d') |
                 ForEach-Object { $_.Value } | Sort-Object -Unique
        $maxDate = if ($dates.Count) { $dates[-1] } else { 'none' }
        $stale = [bool]$d.meta.stale
        $failed = [int]$d.meta.failed
        $detail = "widgets=$(@($d.widgets).Count) failed=$failed stale=$stale errs=$errs data=$maxDate"
        if ($failed -gt 0 -or $errs -gt 0 -or $maxDate -eq 'none') {
            Add-Result '看板' $b.slug 'FAIL' $detail; $bad++
        } elseif ($stale) {
            Add-Result '看板' $b.slug 'WARN' $detail
        } else {
            Add-Result '看板' $b.slug 'PASS' $detail
        }
    }
    if ($bad -eq 0) { Add-Result '看板' '合计' 'PASS' ($boards.Count.ToString() + ' 页无失败组件') }
} catch { Add-Result '看板' '原生看板' 'FAIL' $_.Exception.Message }

# ═══════════════════════════════════════════════════════════ 6. 关键看板数据
Write-Section '6. 关键看板数据新鲜度'
function Add-DataCheck([string]$Name, [string]$Url, [scriptblock]$Pick) {
    try {
        $j = Get-Json $Url
        $v = & $Pick $j
        Add-Result '数据' $Name 'PASS' $v
    } catch { Add-Result '数据' $Name 'WARN' $_.Exception.Message }
}

Add-DataCheck 'VMAX CXO 原版' "$Api/api/vmax-cxo-dashboard/status" { param($j) "updated=$($j.updated_at) components=$($j.cached_components)" }
Add-DataCheck 'CXO 日粒度' "$Api/api/vmax-cxo-daily/status" { param($j) "days=$($j.stored_days) newest=$($j.newest)" }
Add-DataCheck 'OLIB SOC' "$Api/api/olib-soc-dashboard/status" { param($j) "days=$($j.stored_days)/$($j.expected_days) missing=$($j.missing_days) incomplete=$($j.incomplete_days) newest=$($j.newest)" }
Add-DataCheck '可用率-多维' "$Api/api/avail-ms/matrix" { param($j) "data_through=$($j.meta.data_through)" }
Add-DataCheck '可用率-FLM' "$Api/api/avail-flm/matrix" { param($j) "data_through=$($j.meta.data_through)" }
Add-DataCheck '可用率-县维度' "$Api/api/avail-county/matrix" { param($j) "data_through=$($j.meta.data_through)" }
Add-DataCheck 'MW 实时' "$Api/api/mw-ume/realtime" { param($j) "selected=$($j.meta.selected_date) granularity=$($j.meta.granularity)" }
Add-DataCheck '丢包看板' "$Api/api/packet-loss-dashboard/overview" { param($j) "generated=$($j.generated_at) sites=$($j.total_sites)" }
Add-DataCheck '电池看板' "$Api/api/battery-dashboard/all" { param($j) "last_sync=$($j.meta.last_sync_ts) batteries=$($j.kpi.total_batteries)" }
Add-DataCheck 'SOC 趋势' "$Api/api/soc-trend/meta" { param($j) "updated=$($j.updated_at) last_day=$($j.hour_days[-1]) status=$($j.last_run_status)" }
Add-DataCheck 'MS SLA' "$Api/api/ms-sla-dashboard/overview" { param($j) "last_sync=$($j.meta.last_sync_time)" }
Add-DataCheck 'CN KPI' "$Api/api/cn-kpi/overview" { param($j) "widgets=$(@($j.widgets).Count) failed=$($j.meta.failed) stale=$($j.meta.stale)" }
Add-DataCheck 'CN 小时 KPI' "$Api/api/cn-kpi-5mins/overview" { param($j) "widgets=$(@($j.widgets).Count) failed=$($j.meta.failed)" }
Add-DataCheck 'eFlow 调度' "$Api/api/eflow/status" { param($j) "scheduler=$($j.scheduler_running) next=$($j.next_run)" }

# ═══════════════════════════════════════════════════════════ 7. WhatsApp 链路
Write-Section '7. WhatsApp 链路'
$botDir = Join-Path $Root 'apps\whatsapp-kb-bot'
foreach ($pair in @(@{ Name = 'bot'; File = 'bot_heartbeat.json' }, @{ Name = 'notify'; File = 'notify_heartbeat.json' })) {
    $hbFile = Join-Path $botDir ('data\' + $pair.File)
    if (-not (Test-Path -LiteralPath $hbFile)) { Add-Result 'WhatsApp' ($pair.Name + ' 心跳') 'FAIL' '心跳文件不存在'; continue }
    try {
        $hb = Get-Content -LiteralPath $hbFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $age = [Math]::Round(((Get-Date).ToUniversalTime() - ([datetime]$hb.at).ToUniversalTime()).TotalSeconds, 0)
        $detail = "connected=$($hb.connected) age=${age}s pid=$($hb.pid)"
        if ($hb.connected -and $age -le 300) { Add-Result 'WhatsApp' ($pair.Name + ' 心跳') 'PASS' $detail }
        else { Add-Result 'WhatsApp' ($pair.Name + ' 心跳') 'FAIL' $detail }
    } catch { Add-Result 'WhatsApp' ($pair.Name + ' 心跳') 'FAIL' $_.Exception.Message }
}
try {
    $tasks = @(Get-ScheduledTask -TaskName 'WhatsAppKB_*' -ErrorAction Stop)
    $wrong = @($tasks | Where-Object { $_.Actions[0].Arguments -and ($_.Actions[0].Arguments -notmatch [regex]::Escape($Root)) })
    $disabled = @($tasks | Where-Object { $_.State -eq 'Disabled' })
    if ($wrong.Count -eq 0 -and $disabled.Count -eq 0) {
        Add-Result 'WhatsApp' '计划任务' 'PASS' ($tasks.Count.ToString() + ' 个均指向本树且启用')
    } else {
        Add-Result 'WhatsApp' '计划任务' 'WARN' ("错路径=" + $wrong.Count + " 停用=" + $disabled.Count)
    }
} catch { Add-Result 'WhatsApp' '计划任务' 'SKIP' $_.Exception.Message }

# ═══════════════════════════════════════════════════════════ 8. 外部依赖（只报告）
Write-Section '8. 外部依赖（不判失败）'
if (Test-Tcp 'exchmail.orange-cit.ci' 443 5000) { Add-Result '外部' 'EWS exchmail:443' 'PASS' '可达' }
else { Add-Result '外部' 'EWS exchmail:443' 'WARN' 'TCP 5s 不可达 → 邮件类任务会失败（上游/网络）' }

# ═══════════════════════════════════════════════════════════ 9. 可选：真实渲染
if ($Render) {
    Write-Section '9. 真实渲染（Playwright）'
    $py = Get-Command python -ErrorAction SilentlyContinue
    $renderer = Join-Path $PSScriptRoot 'post-cutover-render.py'
    if (-not $py) {
        Add-Result '渲染' 'python' 'SKIP' 'PATH 里没有 python'
    } elseif (-not (Test-Path -LiteralPath $renderer)) {
        Add-Result '渲染' '渲染脚本' 'SKIP' '缺少 post-cutover-render.py'
    } else {
        try {
            $out = & $py.Source $renderer --base 'http://127.0.0.1/myapp/' 2>&1
            $json = ($out -join "`n") | ConvertFrom-Json
            foreach ($p in $json) {
                $detail = "console=$($p.console_errors) api4xx5xx=$(@($p.bad_api).Count) text=$($p.text_len)"
                if ($p.console_errors -gt 0 -or @($p.bad_api).Count -gt 0) { Add-Result '渲染' $p.page 'WARN' $detail }
                else { Add-Result '渲染' $p.page 'PASS' $detail }
            }
        } catch { Add-Result '渲染' '渲染' 'WARN' $_.Exception.Message }
    }
}

# ═══════════════════════════════════════════════════════════ 汇总
Write-Section '汇总'
$fail = @($results | Where-Object { $_.Status -eq 'FAIL' })
$warn = @($results | Where-Object { $_.Status -eq 'WARN' })
$pass = @($results | Where-Object { $_.Status -eq 'PASS' })
$skip = @($results | Where-Object { $_.Status -eq 'SKIP' })
Write-Host ('  PASS ' + $pass.Count + '   WARN ' + $warn.Count + '   FAIL ' + $fail.Count + '   SKIP ' + $skip.Count)
if ($fail.Count -gt 0) {
    Write-Host ''
    Write-Host '  失败项：' -ForegroundColor Red
    $fail | ForEach-Object { Write-Host ('   - [' + $_.Area + '] ' + $_.Name + '  ' + $_.Detail) -ForegroundColor Red }
}

# 报告落 TMP（不入库），供事后对比
$reportDir = Join-Path $Root 'TMP\post-cutover-check'
try {
    if (-not (Test-Path -LiteralPath $reportDir)) { New-Item -ItemType Directory -Path $reportDir -Force | Out-Null }
    $report = Join-Path $reportDir ($startedAt.ToString('yyyyMMdd-HHmmss') + '.json')
    [pscustomobject]@{
        host = $env:COMPUTERNAME
        root = $Root
        started_at = $startedAt.ToString('o')
        elapsed_s = [Math]::Round(((Get-Date) - $startedAt).TotalSeconds, 1)
        since_hours = $SinceHours
        summary = @{ pass = $pass.Count; warn = $warn.Count; fail = $fail.Count; skip = $skip.Count }
        results = $results
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $report -Encoding UTF8
    Write-Host ''
    Write-Host ('  报告：' + $report) -ForegroundColor DarkGray
} catch { Write-Host ('  [WARN] 报告写入失败：' + $_.Exception.Message) -ForegroundColor Yellow }

Write-Host ''
if ($fail.Count -gt 0) { exit 1 } else { exit 0 }
