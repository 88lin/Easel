$ErrorActionPreference = 'Stop'
$Profile = 'easel'
$Root = Split-Path -Parent $PSScriptRoot
$LogFile = Join-Path $env:TEMP 'easel-gateway.log'
$ErrorLogFile = Join-Path $env:TEMP 'easel-gateway.error.log'
$ConfigDir = Join-Path $HOME ".openclaw-$Profile"

# ---- gateway 端口：不写死，与 easel/gateway_endpoint.py 同一套优先级 ----------
# OpenClaw 对**非默认 profile** 不用 18789：它按 20000 + fnv1a32(profile) % 40000 分配
# （easel → 37289），并把结果落进 ~/.openclaw-easel/openclaw.json。写死就会「gateway
# 活着、healthz 恒探不通」。解析顺序：环境变量 > openclaw.json > profile 哈希。
function ConvertTo-GatewayPort([string]$Raw) {
    # 照抄 OpenClaw 的 parseGatewayPortEnvValue：纯数字 / `host:端口` / `[IPv6]:端口`
    if ([string]::IsNullOrWhiteSpace($Raw)) { return 0 }
    $text = $Raw.Trim()
    $candidate = $null
    if ($text -match '^\d+$') { $candidate = $text }
    elseif ($text -match '^\[[^\]]+\]:(\d+)$') { $candidate = $Matches[1] }
    elseif ($text -match '^[^:]+:(\d+)$') { $candidate = $Matches[1] }
    if (-not $candidate) { return 0 }
    $port = 0
    if (-not [int]::TryParse($candidate, [ref]$port)) { return 0 }
    if ($port -lt 1 -or $port -gt 65535) { return 0 }
    return $port
}

function Get-ConfiguredPort([string]$Dir) {
    $cfg = Join-Path $Dir 'openclaw.json'
    if (-not (Test-Path -LiteralPath $cfg)) { return 0 }
    try { $json = Get-Content -LiteralPath $cfg -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { return 0 }
    $port = $null
    # -ccontains：JSON 键大小写敏感，与 openclaw（cfg?.gateway?.port）/ Python 侧一致
    if ($json.PSObject.Properties.Name -ccontains 'gateway') { $port = $json.gateway.port }
    # 与 OpenClaw 一致：只认 JSON number（字符串 "18789" / $true 在那边同样被忽略）
    if ($null -eq $port -or $port -is [string] -or $port -is [bool]) { return 0 }
    if ($port -is [double] -and $port -ne [math]::Truncate($port)) { return 0 }
    $int = 0
    if (-not [int]::TryParse([string]$port, [ref]$int)) { return 0 }
    if ($int -lt 1 -or $int -gt 65535) { return 0 }
    return $int
}

function Get-ProfilePort([string]$ProfileName) {
    # 20000 + fnv1a32(profile) % 40000；只有默认 profile 才是 18789。
    # OpenClaw 的 normalizeProfileName 只在判断是否等于 "default" 时转小写比较，
    # 参与哈希的仍是原始大小写——这里必须照办，否则 "Easel"/"easel" 会被当成
    # 同一个 profile，算出跟真实 gateway 不一致的端口。
    $name = $ProfileName.Trim()
    if (-not $name -or $name.ToLowerInvariant() -eq 'default') { return 18789 }
    $hash = [long]2166136261
    foreach ($byte in [System.Text.Encoding]::UTF8.GetBytes($name)) {
        # 必须每步显式 [long]：PS 的 -bxor 会溢出到 UInt64，而 UInt64 * Int32 会再被
        # 提升成 Double（=> "6.09E+23 无法转换为 UInt64"）。
        $xor = [long]($hash -bxor [long]$byte)
        $hash = [long](($xor * [long]16777619) -band [long]4294967295)
    }
    return [int](20000 + ($hash % 40000))
}

# Easel 自己的覆盖要透传给 OpenClaw（gateway 进程只认 OPENCLAW_GATEWAY_PORT，优先级最高）
if ($env:EASEL_GATEWAY_PORT) { $env:OPENCLAW_GATEWAY_PORT = $env:EASEL_GATEWAY_PORT }

$Port = ConvertTo-GatewayPort $env:OPENCLAW_GATEWAY_PORT
if (-not $Port) { $Port = ConvertTo-GatewayPort $env:EASEL_GATEWAY_PORT }
if (-not $Port) { $Port = Get-ConfiguredPort $ConfigDir }
if (-not $Port) { $Port = Get-ProfilePort $Profile }

function Test-Gateway {
    try { Invoke-WebRequest "http://127.0.0.1:$Port/healthz" -UseBasicParsing -TimeoutSec 2 | Out-Null; return $true }
    catch { return $false }
}

function Get-GatewayProcess {
    Get-CimInstance Win32_Process -Filter "Name = 'node.exe'" |
        Where-Object { $_.CommandLine -match "openclaw.*--profile\s+$Profile.*gateway" } |
        Select-Object -First 1
}

function Stop-Gateway {
    $process = Get-GatewayProcess
    if ($process) { Stop-Process -Id $process.ProcessId -Force; Write-Host '[easel] Gateway stopped' }
    else { Write-Host '[easel] Gateway was not running' }
}

switch ($args[0]) {
    'start' {
        if (Test-Gateway) { Write-Host '[easel] Gateway already running'; break }
        New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
        Write-Host "[easel] Starting Easel gateway (profile: $Profile, port: $Port)..."
        $command = "openclaw --profile $Profile gateway run --force --allow-unconfigured --bind loopback"
        Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-Command', $command `
            -WorkingDirectory $Root -RedirectStandardOutput $LogFile -RedirectStandardError $ErrorLogFile -WindowStyle Hidden | Out-Null
        $ready = $false
        1..20 | ForEach-Object {
            if (-not $ready) {
                if (Test-Gateway) { $ready = $true }
                else { Start-Sleep -Seconds 1 }
            }
        }
        if ($ready) { Write-Host '[easel] Gateway started' }
        else { Write-Error "Gateway 启动失败；请检查 $LogFile 和 $ErrorLogFile"; exit 1 }
    }
    'stop' { Stop-Gateway }
    'restart' { Stop-Gateway; Start-Sleep -Seconds 2; & $PSCommandPath start }
    'status' {
        if (Test-Gateway) { Write-Host "[easel] Gateway running (profile: $Profile, port: $Port)" }
        else { Write-Host "[easel] Gateway not running (profile: $Profile, port: $Port)" }
    }
    'logs' { Get-Content $LogFile -Wait }
    default { Write-Host 'Usage: gateway.ps1 {start|stop|restart|status|logs}'; exit 1 }
}
