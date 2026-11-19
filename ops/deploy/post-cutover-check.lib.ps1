# post-cutover-check.lib.ps1 —— post-cutover-check.ps1 的通用探测函数库
# =============================================================================
# 由 post-cutover-check.ps1 dot-source 引入；这里只定义函数，不执行任何检查。
# 拆出来的原因：主脚本原本 548 行，超过「新增文件 ≤500 行」的约定（AGENTS.md 三.2）。
# 函数不依赖调用方变量，也不读写文件；HTTP / 端口 / 进程探测都比较轻。
# =============================================================================

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

# 服务正在重启时第一次探测必然失败（2026-11-19 实测：7090 在 11:21:03 才起来，
# 检查在 11:20:56 探到「未监听 / 502」）。给一次 6 秒后的重试，避免把重启误报成故障。
function Get-CodeRetry([string]$Url) {
    $code = Get-Code $Url
    if ($code -eq 200) { return $code }
    Start-Sleep -Seconds 6
    return (Get-Code $Url)
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
