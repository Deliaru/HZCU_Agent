[CmdletBinding()]
param(
    [ValidateSet("Demo", "Real")]
    [string]$ModelMode = "Demo",
    [string]$ModelConfig = "API.txt",
    [int]$ApiPort = 18000,
    [int]$WebPort = 13000,
    [string]$ApiHost = "0.0.0.0",
    [string]$WebHost = "0.0.0.0",
    [switch]$LocalAdmin,
    [switch]$SkipInstall,
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$apiRoot = Join-Path $projectRoot "apps\api"
$webRoot = Join-Path $projectRoot "apps\web"
$windowsVenv = Join-Path $projectRoot ".venv-windows"
$windowsPython = Join-Path $windowsVenv "Scripts\python.exe"
$configExample = Join-Path $projectRoot "config\windows.env.example"
$configLocal = Join-Path $projectRoot "config\windows.env"

function Write-Step {
    param([string]$Message)
    Write-Host "[hzcu] $Message" -ForegroundColor Cyan
}

function Assert-LastExitCode {
    param([string]$Action)
    if ($LASTEXITCODE -ne 0) {
        throw "$Action 失败，退出码：$LASTEXITCODE"
    }
}

function Import-EnvFile {
    param([string]$Path)

    foreach ($rawLine in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $line = $rawLine.Trim()
        if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith("#")) {
            continue
        }
        if ($line -notmatch '^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            throw "Windows 配置文件存在无法解析的行：$rawLine"
        }

        $name = $Matches[1]
        $value = $Matches[2].Trim()
        if ($value.Length -ge 2) {
            $first = $value.Substring(0, 1)
            $last = $value.Substring($value.Length - 1, 1)
            if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

function Get-RequiredCommand {
    param([string]$Name)
    $command = Get-Command $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $command) {
        throw "找不到 $Name。请先安装并加入 PATH。"
    }
    return $command.Source
}

function Test-PrivateIPv4Address {
    param([string]$Address)

    $parsed = $null
    if (-not [System.Net.IPAddress]::TryParse($Address, [ref]$parsed)) {
        return $false
    }
    $bytes = $parsed.GetAddressBytes()
    if ($bytes.Length -ne 4) {
        return $false
    }
    return (
        $bytes[0] -eq 10 -or
        ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) -or
        ($bytes[0] -eq 192 -and $bytes[1] -eq 168)
    )
}

function Get-LanIPv4Addresses {
    $preferred = @(
        Get-NetIPConfiguration -ErrorAction SilentlyContinue |
            Where-Object {
                $null -ne $_.IPv4Address -and
                $null -ne $_.IPv4DefaultGateway -and
                $_.NetAdapter.Status -eq "Up"
            } |
            ForEach-Object { $_.IPv4Address.IPAddress } |
            Where-Object { Test-PrivateIPv4Address -Address $_ }
    )
    if ($preferred.Count -gt 0) {
        return @($preferred | Sort-Object -Unique)
    }

    return @(
        Get-NetIPAddress -AddressFamily IPv4 -AddressState Preferred -ErrorAction SilentlyContinue |
            ForEach-Object { $_.IPAddress } |
            Where-Object { Test-PrivateIPv4Address -Address $_ } |
            Sort-Object -Unique
    )
}

function Test-PortAvailable {
    param([int]$Port)
    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
    return $null -eq $listener
}

function Wait-ForHttp {
    param(
        [string]$Uri,
        [int]$TimeoutSeconds = 60,
        [System.Diagnostics.Process]$Process
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if ($null -ne $Process -and $Process.HasExited) {
            throw "服务进程提前退出，无法访问 $Uri。退出码：$($Process.ExitCode)"
        }
        try {
            $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) {
                return
            }
        } catch {
            # The service may still be compiling or binding its port.
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)

    throw "等待服务超时：$Uri"
}

function Stop-ChildProcess {
    param([System.Diagnostics.Process]$Process)
    if ($null -ne $Process -and -not $Process.HasExited) {
        Write-Step "停止进程 $($Process.Id)"
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
    }
}

function Resolve-ConfigPath {
    param([string]$Path)
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return (Resolve-Path -LiteralPath $Path).Path
    }
    return (Resolve-Path -LiteralPath (Join-Path $projectRoot $Path)).Path
}

function ConvertTo-StartProcessArguments {
    param([string[]]$Arguments)

    return ($Arguments | ForEach-Object {
        if ($_ -match '[\s"]') {
            '"' + $_.Replace('"', '\"') + '"'
        } else {
            $_
        }
    }) -join ' '
}

Set-Location $projectRoot

if (Test-Path -LiteralPath $configLocal) {
    Write-Step "加载 Windows 配置：config\windows.env"
    Import-EnvFile -Path $configLocal
} elseif (Test-Path -LiteralPath $configExample) {
    Write-Step "加载 Windows 默认配置：config\windows.env.example"
    Import-EnvFile -Path $configExample
} else {
    throw "缺少 Windows 配置模板：$configExample"
}

# A copied pilot database may contain an encrypted admin model configuration.
# Reuse the local development secret when it is present, without printing it
# or committing it. An explicit environment variable always takes precedence.
if ([string]::IsNullOrWhiteSpace($env:HZCU_MODEL_CONFIG_SECRET)) {
    $localSecretPath = Join-Path $projectRoot "data\local_auth.secret"
    if (Test-Path -LiteralPath $localSecretPath) {
        $localSecret = (Get-Content -LiteralPath $localSecretPath -Raw -Encoding UTF8).Trim()
        if ($localSecret.Length -ge 32) {
            $env:HZCU_MODEL_CONFIG_SECRET = $localSecret
            Write-Step "复用 data\local_auth.secret 解密本地模型配置（不会输出密钥）"
        }
    }
}

$pythonHost = Get-RequiredCommand -Name "python"
$nodeCommand = Get-RequiredCommand -Name "node"
$npmCommand = Get-RequiredCommand -Name "npm.cmd"

$pythonVersion = & $pythonHost -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
Assert-LastExitCode -Action "检查 Python 版本"
$pythonParts = $pythonVersion.Trim().Split('.')
if ([int]$pythonParts[0] -lt 3 -or ([int]$pythonParts[0] -eq 3 -and [int]$pythonParts[1] -lt 12)) {
    throw "项目需要 Python 3.12+，当前为 $pythonVersion"
}

$nodeVersion = & $nodeCommand --version
Assert-LastExitCode -Action "检查 Node.js 版本"
$nodeMajor = [int](($nodeVersion.Trim() -replace '^v', '').Split('.')[0])
if ($nodeMajor -lt 24) {
    throw "项目需要 Node.js 24+，当前为 $nodeVersion"
}

if (-not (Test-PortAvailable -Port $ApiPort)) {
    throw "API 端口 $ApiPort 已被占用，请换端口或先停止已有服务。"
}
if (-not (Test-PortAvailable -Port $WebPort)) {
    throw "Web 端口 $WebPort 已被占用，请换端口或先停止已有服务。"
}

# Keep URL-related settings aligned with command-line ports even when the local
# config file still contains the template defaults.
$lanAddresses = @(Get-LanIPv4Addresses)
$advertisedHost = if ($WebHost -eq "0.0.0.0" -and $lanAddresses.Count -gt 0) {
    $lanAddresses[0]
} elseif ($WebHost -eq "0.0.0.0") {
    "127.0.0.1"
} else {
    $WebHost
}
$configuredCorsOrigins = @(
    $env:HZCU_CORS_ORIGINS -split "," |
        ForEach-Object { $_.Trim() } |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
)
$generatedCorsOrigins = @(
    "http://127.0.0.1:$WebPort"
    "http://localhost:$WebPort"
    $lanAddresses | ForEach-Object { "http://${_}:$WebPort" }
)
$devOriginHosts = @(
    "127.0.0.1"
    "localhost"
    $lanAddresses
)

$env:HZCU_PUBLIC_API_BASE_URL = "http://${advertisedHost}:$ApiPort"
$env:HZCU_WEB_APP_URL = "http://${advertisedHost}:$WebPort"
$env:HZCU_CORS_ORIGINS = (@($configuredCorsOrigins + $generatedCorsOrigins) | Sort-Object -Unique) -join ","
$env:HZCU_ALLOWED_DEV_ORIGINS = (@($devOriginHosts) | Sort-Object -Unique) -join ","
$env:NEXT_PUBLIC_API_BASE_URL = "/api/v1"
if ($LocalAdmin) {
    $env:HZCU_AUTH_MODE = "anonymous"
    $env:HZCU_LOCAL_ADMIN_ENABLED = "true"
    $env:HZCU_AUTH_COOKIE_SECURE = "false"
    Write-Step "启用本地管理员模式（仅监听 $ApiHost）"
}

New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot "data") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot "data\snapshots") | Out-Null

if (-not (Test-Path -LiteralPath $windowsPython)) {
    Write-Step "创建 Windows Python 虚拟环境：.venv-windows"
    & $pythonHost -m venv $windowsVenv
    Assert-LastExitCode -Action "创建 Windows Python 虚拟环境"
}

if (-not $SkipInstall) {
    Write-Step "安装/更新 API Python 依赖"
    & $windowsPython -m pip install --disable-pip-version-check --upgrade pip
    Assert-LastExitCode -Action "升级 pip"
    & $windowsPython -m pip install --disable-pip-version-check -e "$apiRoot[dev]"
    Assert-LastExitCode -Action "安装 API Python 依赖"
    # Windows does not ship the IANA time-zone database used by zoneinfo.
    & $windowsPython -m pip install --disable-pip-version-check tzdata
    Assert-LastExitCode -Action "安装 Windows 时区数据库"

    Write-Step "安装/校验 Web npm 依赖"
    Push-Location $webRoot
    try {
        & $npmCommand ci --no-audit --no-fund
        Assert-LastExitCode -Action "安装 Web npm 依赖"
    } finally {
        Pop-Location
    }
} elseif (-not (Test-Path -LiteralPath (Join-Path $webRoot "node_modules\.bin\next.cmd"))) {
    throw "-SkipInstall 已指定，但 Web 依赖不存在，请先不带 -SkipInstall 运行一次。"
}

Write-Step "执行数据库迁移"
& $windowsPython -m alembic -c (Join-Path $apiRoot "alembic.ini") upgrade head
Assert-LastExitCode -Action "数据库迁移"

$processes = @()
try {
    if ($ModelMode -eq "Real") {
        $modelPath = Resolve-ConfigPath -Path $ModelConfig
        $apiArguments = @(
            "-m", "hzcu_agent.cli", "serve",
            "--host", $ApiHost,
            "--port", "$ApiPort",
            "--model-config", $modelPath,
            "--model-timeout", "180",
            "--anonymous-campus-mirror"
        )
    } else {
        $apiArguments = @(
            "-m", "uvicorn", "hzcu_agent.main:app",
            "--host", $ApiHost,
            "--port", "$ApiPort",
            "--no-access-log"
        )
        if ($Reload) {
            $apiArguments += "--reload"
        }
    }

    Write-Step "启动 API（$ModelMode 模式）"
    $apiProcess = Start-Process -FilePath $windowsPython -ArgumentList (ConvertTo-StartProcessArguments $apiArguments) -WorkingDirectory $projectRoot -NoNewWindow -PassThru
    $processes += $apiProcess
    Wait-ForHttp -Uri "http://127.0.0.1:$ApiPort/api/v1/health" -Process $apiProcess

    $webArguments = @(
        "run", "dev", "--",
        "--webpack",
        "--api-url", "http://127.0.0.1:$ApiPort",
        "--hostname", $WebHost,
        "--port", "$WebPort"
    )
    Write-Step "启动 Web"
    $webProcess = Start-Process -FilePath $npmCommand -ArgumentList (ConvertTo-StartProcessArguments $webArguments) -WorkingDirectory $webRoot -NoNewWindow -PassThru
    $processes += $webProcess
    Wait-ForHttp -Uri "http://127.0.0.1:$WebPort/" -Process $webProcess

    Write-Host ""
    Write-Host "项目已启动（Windows 原生）" -ForegroundColor Green
    Write-Host "本机访问: http://127.0.0.1:$WebPort/"
    if ($lanAddresses.Count -gt 0) {
        foreach ($address in $lanAddresses) {
            Write-Host "局域网访问: http://${address}:$WebPort/"
        }
    } else {
        Write-Host "未检测到私有 IPv4 地址；请用 ipconfig 查看手机可访问的网卡地址。" -ForegroundColor Yellow
    }
    Write-Host "API 健康检查: http://127.0.0.1:$ApiPort/api/v1/health"
    Write-Host "按 Ctrl+C 停止 API 和 Web。"
    Write-Host ""

    while ($true) {
        $running = @($processes | Where-Object { -not $_.HasExited })
        if ($running.Count -eq 0) {
            break
        }
        Start-Sleep -Seconds 1
    }
} finally {
    foreach ($process in $processes) {
        Stop-ChildProcess -Process $process
    }
}
