# =============================================================================
# sync-ui-config-from-01.ps1 —— 把生产（D:\01_olbr_portal）的「全局 UI 配置」
# 同步到本仓库（D:\02_olbr_portal 隔离副本）
# -----------------------------------------------------------------------------
# 背景：01（80/443，生产）与 02（8080/8443，隔离副本）是同一仓库的两个检出，
#       且 02 的 backend 是重构后的域分包布局 —— 代码**不能**整目录 robocopy 覆盖。
#       但「全局 UI 配置」是运行时 JSON，my_app 后端每次请求都现读文件
#       （routers\ui_tool_order.py 的 _read_visibility/_read_order 无缓存），
#       复制过去即对所有人生效，**无需重启**。
#
# 白名单（只同步这几份，其余一律不碰）：
#   backend\data\ui_tool_visibility.json   eflow_qa 页 6 个工具块的全局隐藏配置
#   backend\data\ui_tool_order.json        eflow_qa 页工具块的全局默认顺序
#   backend\data\ui_menu_layout.json       my_app 侧栏目录（分组/顺序/隐藏）全局布局
#
# 刻意**不在**白名单里（02 有自己的实例状态，同步会砸坏）：
#   .env（端口不同）、db\（本地权威表 credentials/task_schedule_config/email_recipients）、
#   log\、TMP\、auth_cache\、portal-default.json、portal auth-data\、nginx conf（02 按 .env 生成）。
#
# 代码/页面级的改动不要用本脚本：走 git（01 推 GitHub，02 里 fetch + merge）。
#
# 与 2026-11-16 初版（写在 D:\01_olbr_portal\scripts\ 下、只用 Copy-Item）相比，
# 这一版补了三件事，都是那次实测暴露出来的：
#   1. **先校验再写**：从 02 自己的代码里取真相源（ui_tool_order.py 的
#      VALID_TOOL_KEYS、App.tsx 的 VALID_PAGES + FINEBI_BOARDS），任何未知 key
#      直接拒绝整批同步，不允许「先复制再说」。理由：前端对不认识的页面键是
#      **静默丢弃**（App.tsx#normalizeMenuLayout），坏了不报错，只会让页面从侧栏消失。
#   2. **原子替换**：先写同目录 .tmp → 校验哈希 → Move-Item 覆盖。直接 Copy-Item
#      覆盖的过程中若有请求读，_read_visibility 会 except 后返回 None，那一瞬间
#      页面表现为「全部显示」。
#   3. **覆盖前备份 + 留痕**：*.bak_<时间>_from01（每份只留最近 3 个，已被
#      .gitignore 的 *.bak_* 覆盖），运行记录追加到 TMP\sync-ui-config.log。
#      被覆盖的若是 02 上改过的目录名 / 隐藏分组，才有得回退。
#
# 用法（默认是试运行，不写任何文件）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File ops\deploy\sync-ui-config-from-01.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File ops\deploy\sync-ui-config-from-01.ps1 -Apply
#
# 可选参数：
#   -SourceRoot / -TargetRoot  换源 / 换目标（默认 01 的 backend\data → 本仓库 02 对应的目录）
#   -AllowUnknownKeys          明知 02 缺这些页面仍要照抄（不推荐，会有页面从侧栏消失）
#   -NoLog                     不写 TMP\sync-ui-config.log
# =============================================================================
[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$AllowUnknownKeys,
    [switch]$NoLog,
    [string]$SourceRoot = 'D:\01_olbr_portal\Task\my_app\backend\data',
    [string]$TargetRoot = ''
)

$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $TargetRoot) { $TargetRoot = Join-Path $RepoRoot 'apps\my_app\backend\data' }

$Files   = @('ui_tool_visibility.json', 'ui_tool_order.json', 'ui_menu_layout.json')
$LogPath = Join-Path $RepoRoot 'TMP\sync-ui-config.log'

# --------------------------- 生产只读守卫 ---------------------------
# 与 ops\deploy\smoke-watcher.ps1 开头同一条纪律：推导出的目标落在生产目录下直接拒绝，
# 免得参数写错把生产配置反向覆盖。
$targetFull = [System.IO.Path]::GetFullPath($TargetRoot)
if ($targetFull.StartsWith('D:\01_olbr_portal', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "拒绝执行：目标目录落在生产 D:\01_olbr_portal 下（生产只读），实际为 $targetFull"
}
if (-not (Test-Path $SourceRoot)) { throw "源目录不存在: $SourceRoot" }
if (-not (Test-Path $TargetRoot)) { throw "目标目录不存在: $TargetRoot（02 副本未就位？）" }

# --------------------------- 工具函数 ---------------------------
function Get-FileHashSafe([string]$path) {
    if (-not (Test-Path $path)) { return $null }
    (Get-FileHash -LiteralPath $path -Algorithm MD5).Hash
}

function Read-JsonFile([string]$path) {
    $raw = Get-Content -LiteralPath $path -Raw -Encoding UTF8
    if (-not $raw) { throw "文件为空: $path" }
    try { return ($raw | ConvertFrom-Json) }
    catch { throw "不是合法 JSON: $path — $($_.Exception.Message)" }
}

# 真相源 1：eflow_qa 的 6 个工具块 key（backend\routers\ui_tool_order.py）
function Get-ValidToolKeys {
    $p = Join-Path $RepoRoot 'apps\my_app\backend\routers\ui_tool_order.py'
    if (-not (Test-Path $p)) { throw "找不到工具块 truth source: $p" }
    $txt = Get-Content -LiteralPath $p -Raw -Encoding UTF8
    $m = [regex]::Match($txt, 'VALID_TOOL_KEYS\s*=\s*frozenset\(\{([\s\S]*?)\}\)')
    if (-not $m.Success) {
        throw "无法从 $p 解析 VALID_TOOL_KEYS —— 守卫取不到真相源必须显式失败，不能当成「不适用」跳过"
    }
    $keys = @([regex]::Matches($m.Groups[1].Value, '"([A-Za-z0-9_-]+)"') | ForEach-Object { $_.Groups[1].Value })
    if ($keys.Count -lt 2) { throw "VALID_TOOL_KEYS 解析结果异常（$($keys.Count) 项），检查 $p 是否改了写法" }
    return $keys
}

# 真相源 2：侧栏合法页面键（frontend\src\App.tsx 的 VALID_PAGES + FINEBI_BOARDS）
function Get-ValidPages {
    $p = Join-Path $RepoRoot 'apps\my_app\frontend\src\App.tsx'
    if (-not (Test-Path $p)) { throw "找不到页面 truth source: $p" }
    $txt = Get-Content -LiteralPath $p -Raw -Encoding UTF8

    $line = [regex]::Match($txt, 'const VALID_PAGES[^\r\n]*').Value
    if (-not $line) { throw "无法从 $p 解析 VALID_PAGES" }
    $pages = @([regex]::Matches($line, "'([A-Za-z0-9_./-]+)'") | ForEach-Object { $_.Groups[1].Value })

    # finebi_<slug> 由 FINEBI_BOARDS 动态注入，不在 VALID_PAGES 的字面量里
    $fbBlock = [regex]::Match($txt, 'const FINEBI_BOARDS[\s\S]*?\n\]').Value
    if (-not $fbBlock) { throw "无法从 $p 解析 FINEBI_BOARDS" }
    $slugs = @([regex]::Matches($fbBlock, "slug:\s*'([^']+)'") | ForEach-Object { $_.Groups[1].Value })
    $pages += @($slugs | ForEach-Object { "finebi_$_" })

    if ($pages.Count -lt 10) { throw "VALID_PAGES 解析结果异常（$($pages.Count) 项），检查 $p 是否改了写法" }
    return $pages
}

function Add-RunLog([string]$msg) {
    if ($NoLog) { return }
    try {
        $dir = Split-Path $LogPath -Parent
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
        Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    }
    catch {
        # 日志写不进去不该让同步失败（沙箱/权限/盘满都可能）
        Write-Host "[警告] 日志写入失败（不影响本次同步）：$($_.Exception.Message)" -ForegroundColor DarkYellow
    }
}

# 备份：同名 .bak_<时间>_from01，校验一致才算成功；每份只留最近 3 个
function Backup-Target([string]$dst) {
    $dir  = Split-Path $dst -Parent
    $name = Split-Path $dst -Leaf
    $bak  = Join-Path $dir ('{0}.bak_{1}_from01' -f $name, (Get-Date -Format 'yyyyMMdd_HHmmss_fff'))
    Copy-Item -LiteralPath $dst -Destination $bak -Force
    if ((Get-FileHashSafe $bak) -ne (Get-FileHashSafe $dst)) { throw "备份校验失败: $bak" }
    Get-ChildItem -LiteralPath $dir -Filter ('{0}.bak_*_from01' -f $name) |
        Sort-Object LastWriteTime -Descending | Select-Object -Skip 3 |
        Remove-Item -Force -ErrorAction SilentlyContinue
    return $bak
}

# 原子替换：.tmp → 校验 → Move-Item（同卷 rename，读方要么看到旧文件要么看到新文件）
function Copy-Atomic([string]$src, [string]$dst) {
    $dir = Split-Path $dst -Parent
    $tmp = Join-Path $dir ('{0}.{1}.tmp' -f (Split-Path $dst -Leaf), $PID)
    $want = Get-FileHashSafe $src
    try {
        Copy-Item -LiteralPath $src -Destination $tmp -Force
        if ((Get-FileHashSafe $tmp) -ne $want) { throw "临时文件校验失败: $tmp" }
        # 目标偶尔被后端短暂打开（读配置），重试几次比直接抛错友好
        for ($i = 0; $i -lt 3; $i++) {
            try { Move-Item -LiteralPath $tmp -Destination $dst -Force; break }
            catch { if ($i -eq 2) { throw }; Start-Sleep -Milliseconds 150 }
        }
        if ((Get-FileHashSafe $dst) -ne $want) { throw "替换后校验失败: $dst" }
    }
    finally {
        if (Test-Path $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
    }
}

# 内容对比（试运行时打印，方便确认差异是不是预期的）
function Format-JsonCompact($obj) {
    try { return (($obj | ConvertTo-Json -Depth 10 -Compress) -replace '\s+', ' ') }
    catch { return '(无法序列化)' }
}

# --------------------------- 一、先校验整批 ---------------------------
$validToolKeys = Get-ValidToolKeys
$validPages    = Get-ValidPages

Write-Host "仓库根  : $RepoRoot"
Write-Host "源(01)  : $SourceRoot"
Write-Host "目标(02): $TargetRoot"
Write-Host "模式    : $(if ($Apply) { 'APPLY（会写文件）' } else { '试运行（不写文件）' })"
Write-Host ''

$errors = New-Object System.Collections.Generic.List[string]
$plans  = New-Object System.Collections.Generic.List[object]

foreach ($f in $Files) {
    $src = Join-Path $SourceRoot $f
    if (-not (Test-Path $src)) {
        Write-Host "[跳过] 源不存在：$f"
        continue
    }

    $obj = $null
    try { $obj = Read-JsonFile $src }
    catch { $errors.Add("$f：$($_.Exception.Message)"); continue }

    switch ($f) {
        'ui_tool_order.json' {
            $keys = @($obj.keys)
            if ($keys.Count -ne $validToolKeys.Count) {
                $errors.Add("$f：keys 应有 $($validToolKeys.Count) 项，实际 $($keys.Count) 项")
                break
            }
            $bad = @($keys | Where-Object { $validToolKeys -notcontains $_ })
            if ($bad.Count) { $errors.Add("$f：02 没有这些工具块 key —— $($bad -join ', ')") }
            $dup = @($keys | Group-Object | Where-Object Count -gt 1 | ForEach-Object Name)
            if ($dup.Count) { $errors.Add("$f：key 重复 —— $($dup -join ', ')") }
        }
        'ui_tool_visibility.json' {
            $hidden = @($obj.hidden)
            $bad = @($hidden | Where-Object { $validToolKeys -notcontains $_ })
            if ($bad.Count) { $errors.Add("$f：02 没有这些工具块 key —— $($bad -join ', ')") }
        }
        'ui_menu_layout.json' {
            $groups = @($obj.groups)
            if (-not $groups.Count) { $errors.Add("$f：groups 为空"); break }
            $items = @()
            foreach ($g in $groups) { $items += @($g.items) }
            $bad = @($items | Where-Object { $validPages -notcontains $_ -and $_ -ne $null })
            if ($bad.Count) {
                $msg = "$f：02 的 App.tsx 里没有这些页面键 —— " + ($bad -join ', ')
                if ($AllowUnknownKeys) { Write-Host "[警告] $msg（已按 -AllowUnknownKeys 放行，这些页面会在 02 侧栏消失）" -ForegroundColor Yellow }
                else { $errors.Add($msg) }
            }
        }
    }
    $plans.Add([pscustomobject]@{ Name = $f; Src = $src; Dst = (Join-Path $TargetRoot $f) })
}

if ($errors.Count) {
    Write-Host '校验未通过，整批同步已取消（一个文件都没写）：' -ForegroundColor Red
    foreach ($e in $errors) { Write-Host "  - $e" -ForegroundColor Red }
    Write-Host ''
    Write-Host '这类错正是「复制过去了、页面却没了」的成因：前端对未知页面键是静默丢弃。'
    Write-Host '请先确认 01 与 02 的代码版本差在哪（走 git 合代码），再同步配置。'
    exit 1
}
Write-Host "校验通过：工具块 key $($validToolKeys.Count) 个、页面 key $($validPages.Count) 个均与 02 代码一致。"
Write-Host ''

# --------------------------- 二、逐个比对 / 应用 ---------------------------
$copied = 0; $same = 0; $errors2 = New-Object System.Collections.Generic.List[string]; $pending = @()

foreach ($plan in $plans) {
    $h1 = Get-FileHashSafe $plan.Src
    $h2 = Get-FileHashSafe $plan.Dst
    if ($h1 -eq $h2) {
        Write-Host "[一致] $($plan.Name)"
        $same++
        continue
    }

    if ($h2) { Write-Host "[差异] $($plan.Name)" } else { Write-Host "[新增] $($plan.Name)（02 上还没有这份配置）" }
    $srcObj = Read-JsonFile $plan.Src
    $dstObj = if ($h2) { Read-JsonFile $plan.Dst } else { $null }
    Write-Host "   01: $(Format-JsonCompact $srcObj)"
    Write-Host "   02: $(Format-JsonCompact $dstObj)"

    if (-not $Apply) { $pending += $plan.Name; continue }

    try {
        if ($h2) {
            $bak = Backup-Target $plan.Dst
            Write-Host "   -> 已备份 $(Split-Path $bak -Leaf)"
        }
        Copy-Atomic $plan.Src $plan.Dst
        Write-Host '   -> 已原子替换并校验一致（后端现读文件，立即生效，无需重启）'
        Add-RunLog ("sync-ui-config: {0}  01={1}  old02={2}  by={3}" -f $plan.Name, $h1.Substring(0, 8), $(if ($h2) { $h2.Substring(0, 8) } else { 'new' }), $env:USERNAME)
        $copied++
    }
    catch {
        $errors2.Add("$($plan.Name)：$($_.Exception.Message)")
    }
}

Write-Host ''
if ($Apply) {
    Write-Host "完成：替换 $copied 份，本就一致 $same 份。"
    if (-not $NoLog) { Write-Host "运行记录：$LogPath" }
    if ($errors2.Count) {
        foreach ($e in $errors2) { Write-Host "  ! $e" -ForegroundColor Red }
        exit 2
    }
} else {
    if ($pending.Count) {
        Write-Host ("以上是试运行结果（未写入）。待替换：{0}" -f ($pending -join ', '))
        Write-Host '确认无误后加 -Apply 实际执行：'
        Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Apply"
    } else {
        Write-Host '试运行结果：全部一致，没有需要同步的文件。'
    }
}
exit 0
