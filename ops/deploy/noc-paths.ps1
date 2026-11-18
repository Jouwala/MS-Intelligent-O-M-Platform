<#
noc-paths.ps1 —— NOC_HOME 外部目录解析（ZIP 交付 Phase 1，P1-6）
=============================================================================
start_all.ps1 / stop_all.ps1 共同点源（dot-source 引入），只做路径解析，
不启动、不停止任何东西。改动这里 = 改动两处行为，须经评审。

两种模式（只看进程环境变量 NOC_HOME 是否设置）：
· legacy（未设 NOC_HOME）：全部路径与旧行为一致，全部由 $Root 派生。
便携副本（D:\02）走这里，行为逐行不变。
· external（设了 NOC_HOME）：config/data/logs/runtime 一律在部署根下，
版本目录（$Root）在启动后零写入；写目标落在版本目录内会直接抛错。

NOC_HOME 与 NOC_*_DIR 只从读，写在.env 里无效——因为.env
文件本身的位置就靠它们定。由首次启动脚本/部署包装器在拉起前设置。

变量（全部可选；未设时按 NOC_HOME 派生默认值）：
NOC_HOME 部署根（下有 versions/、current、runtime/、config/、data/、logs/）
NOC_CONFIG_DIR 外部配置目录（默认 <NOC_HOME>\config：.env、nginx.conf、certs/）
NOC_DATA_DIR 外部数据目录（默认 <NOC_HOME>\data：pgdata、portal-auth-data、bot-data）
NOC_LOGS_DIR 外部日志目录（默认 <NOC_HOME>\logs：pid 文件、进程日志、nginx 日志）
NOC_RUNTIME_DIR 运行时目录（默认 <NOC_HOME>\runtime：python312、node、postgresql16、nginx）
NOC_ENV_FILE.env 文件路径（默认 <NOC_CONFIG_DIR>\.env）

PowerShell 5.1 兼容：不用三元、??、::new()、$IsWindows。
#>

function Test-PathUnder {
param([string]$Base, [string]$Path)
# 边界感知的前缀判定：相等或落在子目录内才算“在下面”；
# 盘符/分隔符/大小写先归一，避免 C:\Foo 与 C:\FooBar 误判。
$normBase = ([System.IO.Path]::GetFullPath($Base) -replace '/', '\').TrimEnd('\').ToLowerInvariant()
$normPath = ([System.IO.Path]::GetFullPath($Path) -replace '/', '\').TrimEnd('\').ToLowerInvariant()
if ($normPath -eq $normBase) { return $true}
return $normPath.StartsWith($normBase + '\', [StringComparison]::Ordinal)
}

function Get-ProcessEnvOrDefault {
param([string]$Name, [string]$Default)
$v = [Environment]::GetEnvironmentVariable($Name, 'Process')
if ([string]::IsNullOrWhiteSpace($v)) { return $Default}
return $v
}

function Resolve-NginxDir {
param([string]$Root, [string]$RuntimeDir, [bool]$PreferRuntime)
$rootNginx = Join-Path $Root 'nginx-1.28.0'
$runtimeNginx = Join-Path $RuntimeDir 'nginx-1.28.0'
if ($PreferRuntime) { $candidates = @($runtimeNginx, $rootNginx)}
else { $candidates = @($rootNginx, $runtimeNginx)}
foreach ($c in $candidates) {
if (Test-Path -LiteralPath (Join-Path $c 'nginx.exe')) { return $c}
}
return $candidates[0]
}

function Get-NocPaths {
param([string]$Root)
$Root = ([System.IO.Path]::GetFullPath($Root)).TrimEnd('\')
$nocHome = [Environment]::GetEnvironmentVariable('NOC_HOME', 'Process')
$external = -not [string]::IsNullOrWhiteSpace($nocHome)
if ($external) {
$nocHome = ([System.IO.Path]::GetFullPath($nocHome)).TrimEnd('\')
$configDir = Get-ProcessEnvOrDefault 'NOC_CONFIG_DIR' (Join-Path $nocHome 'config')
$dataDir = Get-ProcessEnvOrDefault 'NOC_DATA_DIR' (Join-Path $nocHome 'data')
$logsDir = Get-ProcessEnvOrDefault 'NOC_LOGS_DIR' (Join-Path $nocHome 'logs')
$runtimeDir = Get-ProcessEnvOrDefault 'NOC_RUNTIME_DIR' (Join-Path $nocHome 'runtime')
$envFile = Get-ProcessEnvOrDefault 'NOC_ENV_FILE' (Join-Path $configDir '.env')
$tmpDir = $logsDir
$pgData = Join-Path $dataDir 'pgdata'
$authDir = Join-Path $dataDir 'portal-auth-data'
$botDataDir = Join-Path $dataDir 'bot-data'
$nginxDir = Resolve-NginxDir -Root $Root -RuntimeDir $runtimeDir -PreferRuntime $true
$nginxConfOut = Join-Path $configDir 'nginx.conf'
$nginxLogsDir = Join-Path $logsDir 'nginx'
$nginxTempDir = Join-Path $logsDir 'nginx-temp'
$certsDir = Join-Path $configDir 'certs'
} else {
$nocHome = ''
$configDir = $Root
$dataDir = Join-Path $Root 'runtime'
$logsDir = Join-Path $Root 'TMP'
$runtimeDir = Join-Path $Root 'runtime'
$envFile = Join-Path $Root '.env'
$tmpDir = Join-Path $Root 'TMP'
$pgData = Join-Path $runtimeDir 'pgdata'
$authDir = Join-Path $runtimeDir 'portal-auth-data'
$botDataDir = Join-Path $Root 'apps\whatsapp-kb-bot\data'
$nginxDir = Resolve-NginxDir -Root $Root -RuntimeDir $runtimeDir -PreferRuntime $false
$nginxConfOut = Join-Path $nginxDir 'conf\nginx.conf'
$nginxLogsDir = Join-Path $nginxDir 'logs'
$nginxTempDir = Join-Path $nginxDir 'temp'
$certsDir = Join-Path $nginxDir 'certs'
}
return [ordered]@{
Mode = $(if ($external) { 'external'} else { 'legacy'})
Root = $Root
NocHome = $nocHome
ConfigDir = $configDir
DataDir = $dataDir
LogsDir = $logsDir
RuntimeDir = $runtimeDir
EnvFile = $envFile
TmpDir = $tmpDir
PgData = $pgData
AuthDir = $authDir
BotDataDir = $botDataDir
NginxDir = $nginxDir
NginxConfOut = $nginxConfOut
NginxLogsDir = $nginxLogsDir
NginxTempDir = $nginxTempDir
CertsDir = $certsDir
NodeExe = (Join-Path $runtimeDir 'node\node.exe')
PyExe = (Join-Path $runtimeDir 'python312\python.exe')
PgBin = (Join-Path $runtimeDir 'postgresql16\bin')
}
}

function Assert-NocPathsOutsideRoot {
param([System.Collections.IDictionary]$Paths)
if ($Paths['Mode'] -ne 'external') { return}
$targets = [ordered]@{
'NOC_HOME' = $Paths['NocHome']
'NOC_CONFIG_DIR' = $Paths['ConfigDir']
'NOC_DATA_DIR' = $Paths['DataDir']
'NOC_LOGS_DIR' = $Paths['LogsDir']
'NOC_RUNTIME_DIR' = $Paths['RuntimeDir']
'NOC_ENV_FILE' = $Paths['EnvFile']
'PgData' = $Paths['PgData']
'AuthDir' = $Paths['AuthDir']
'BotDataDir' = $Paths['BotDataDir']
'NginxConfOut' = $Paths['NginxConfOut']
'NginxLogsDir' = $Paths['NginxLogsDir']
'NginxTempDir' = $Paths['NginxTempDir']
'CertsDir' = $Paths['CertsDir']
}
foreach ($k in $targets.Keys) {
if (Test-PathUnder -Base $Paths['Root'] -Path $targets[$k]) {
throw "外部模式写目标落在版本目录内：$k = $($targets[$k])（版本目录 $($Paths['Root'])），请检查 NOC_HOME / NOC_*_DIR"
}
}
}
