#Requires -Version 5.1
<#
.SYNOPSIS
    Windows service manager for PPT AS (the equivalent of scripts/services.py).

.DESCRIPTION
    scripts/services.py relies on /proc, fcntl.flock and os.killpg, so it can
    only run on Linux. This script manages the same three services on Windows:
        agent     127.0.0.1:8001   uvicorn agent_service.app:app
        backend   127.0.0.1:3002   Express BFF via ts-node
        frontend  0.0.0.0:5174     Vite dev server (/api proxied to 3002)

    Model configuration is read from the project .env only. Host environment
    variables holding model credentials are stripped before the child processes
    start, matching the behaviour of services.py.

    NOTE: messages are intentionally ASCII-only so the script renders correctly
    under Windows PowerShell 5.1, which decodes BOM-less files using the system
    ANSI code page (GBK on zh-CN systems).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\services-windows.ps1 -Action start
#>
[CmdletBinding()]
param(
    [ValidateSet('start', 'stop', 'status', 'restart')]
    [string]$Action = 'start',
    [string]$ProjectRoot
)

# CmdletBinding makes this an advanced script, so param defaults are evaluated in
# the caller's scope where $PSScriptRoot is empty. Resolve it in the body instead.
if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$RunDir      = Join-Path $ProjectRoot 'runtime\services'
$StateFile   = Join-Path $RunDir 'processes.json'
$EnvFile     = Join-Path $ProjectRoot '.env'

$Ports = @{ agent = 8001; backend = 3002; frontend = 5174 }
$HealthUrls = @{
    agent    = 'http://127.0.0.1:8001/api/studio/health'
    backend  = 'http://127.0.0.1:3002/api/health'
    frontend = 'http://127.0.0.1:5174/'
}
$StudioHealthUrl = 'http://127.0.0.1:5174/api/studio/health'

function Write-Step([string]$Text) { Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text)   { Write-Host "    [OK] $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "    [!]  $Text" -ForegroundColor Yellow }
function Write-Err([string]$Text)  { Write-Host "    [X]  $Text" -ForegroundColor Red }

New-Item -ItemType Directory -Force -Path $RunDir | Out-Null

# ---------------------------------------------------------------------------
# Process state
# ---------------------------------------------------------------------------
function Get-Identity([int]$ProcessId) {
    if ($ProcessId -le 1) { return $null }
    try {
        $process = Get-Process -Id $ProcessId -ErrorAction Stop
        return $process.StartTime.ToUniversalTime().ToString('o')
    } catch {
        return $null
    }
}

function Read-State {
    if (-not (Test-Path $StateFile)) { return [pscustomobject]@{} }
    try {
        $raw = Get-Content -LiteralPath $StateFile -Raw -Encoding UTF8
        if (-not $raw.Trim()) { return [pscustomobject]@{} }
        return (ConvertFrom-Json $raw)
    } catch {
        Write-Err "Process state file is corrupt: $StateFile"
        return [pscustomobject]@{}
    }
}

function Write-State($Data) {
    $json = $Data | ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText($StateFile, $json, (New-Object System.Text.UTF8Encoding $false))
}

function Remove-State {
    if (Test-Path $StateFile) { Remove-Item -LiteralPath $StateFile -Force }
}

# ---------------------------------------------------------------------------
# Port and health probes
# ---------------------------------------------------------------------------
function Test-PortOpen([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(400, $false)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-Health([string]$Url) {
    try {
        $response = Invoke-WebRequest -Uri $Url -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
        if ($response.StatusCode -ne 200) { return $null }
        $type = $response.Headers['Content-Type']
        if ($type -and $type -like '*json*') {
            return ($response.Content | ConvertFrom-Json)
        }
        return [pscustomobject]@{ status = 'ok' }
    } catch {
        return $null
    }
}

# ---------------------------------------------------------------------------
# Start / stop
# ---------------------------------------------------------------------------
function Stop-ManagedProcesses {
    $data = Read-State
    $stopped = @()
    foreach ($entry in @($data.PSObject.Properties)) {
        $name = $entry.Name
        $processId = [int]$entry.Value.pid
        if ((Get-Identity $processId) -ne $entry.Value.startTime) { continue }
        try {
            Stop-Process -Id $processId -Force -ErrorAction Stop
            $stopped += $name
        } catch {
            Write-Warn "$name could not be stopped (PID $processId)"
        }
    }
    Start-Sleep -Milliseconds 600
    Remove-State
    if ($stopped.Count -gt 0) {
        Write-Ok ('Stopped: ' + ($stopped -join ', '))
    } else {
        Write-Ok 'No processes managed by this script were running'
    }
}

function Start-ManagedProcess {
    param(
        [string]$Name,
        [string]$Exe,
        [string[]]$Arguments,
        [string]$WorkingDirectory
    )
    $stdout = Join-Path $RunDir "$Name.log"
    $stderr = Join-Path $RunDir "$Name.err.log"
    $process = Start-Process -FilePath $Exe -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $stamp = Get-Identity $process.Id
    if (-not $stamp) {
        throw "$Name failed to start; see $stdout and $stderr"
    }
    return [pscustomobject]@{ pid = $process.Id; startTime = $stamp }
}

function Wait-Healthy {
    param([string]$Name, [string]$Url, [int]$TimeoutSeconds = 90)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $state = Get-Health $Url
        if ($null -ne $state) { return $state }
        Start-Sleep -Milliseconds 500
    }
    throw "$Name timed out after $TimeoutSeconds seconds; see runtime\services\$Name.log"
}

function Invoke-Preflight {
    if (-not (Test-Path $EnvFile)) {
        throw 'Missing .env. Run scripts\setup-windows.ps1 first, then fill in the model configuration.'
    }
    $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    if (-not $nodeCommand) { $nodeCommand = Get-Command node -ErrorAction SilentlyContinue }
    if (-not $nodeCommand) { throw 'Node.js not found. Install Node.js 22.' }
    $nodePath = $nodeCommand.Source

    foreach ($relative in @('frontend\node_modules\vite\bin\vite.js', 'backend\node_modules\ts-node\dist\bin.js')) {
        if (-not (Test-Path (Join-Path $ProjectRoot $relative))) {
            throw "Missing Node dependency ($relative). Run pnpm install first."
        }
    }
    if (-not (Test-Path (Join-Path $ProjectRoot 'packages\pptd-runtime\build\pptd-runtime.js'))) {
        throw 'Missing the PPTD browser runtime. Run pnpm build:pptd first.'
    }

    $venvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $venvPython)) {
        throw 'Missing the Python virtual environment (.venv\Scripts\python.exe). Run scripts\setup-windows.ps1 first.'
    }
    foreach ($module in @('uvicorn', 'fastapi', 'agentscope', 'pptx', 'dotenv', 'fitz')) {
        & $venvPython -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('$module') else 1)" 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw "The virtual environment is missing the '$module' module. Re-run scripts\setup-windows.ps1."
        }
    }

    $occupied = @()
    foreach ($name in $Ports.Keys) {
        if (Test-PortOpen $Ports[$name]) { $occupied += "$name`:$($Ports[$name])" }
    }
    if ($occupied.Count -gt 0) {
        throw ('Ports already in use: ' + ($occupied -join ', ') + '. This script never kills unregistered processes; stop them first.')
    }
    return [pscustomobject]@{ node = $nodePath; venvPython = $venvPython }
}

function Clear-ModelEnvironment {
    # Same as services.py: host model credentials must not override the project .env.
    $prefixes = @('ANTHROPIC_', 'OPENAI_', 'PPT_LLM_', 'PPT_VISION_', 'PPT_EMBEDDING_',
        'PPT_RERANK_', 'PPT_OCR_', 'PPTD_', 'WEKNORA_', 'PPT_IMAGE_', 'PPT_WEB_MCP_',
        'TAVILY_', 'SERPER_', 'SEARXNG_', 'OTEL_')
    $exact = @('PPT_RENDERER_IMAGE', 'LIBREOFFICE_BIN', 'STUDIO_DATA_DIR', 'AGENTSCOPE_STUDIO_URL')
    foreach ($entry in @(Get-ChildItem Env:)) {
        $name = $entry.Name
        if ($exact -contains $name) {
            Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
            continue
        }
        foreach ($prefix in $prefixes) {
            if ($name.StartsWith($prefix)) {
                Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
                break
            }
        }
    }
    $env:PYTHONUNBUFFERED = '1'
    # UTF-8 mode keeps file reads and console output independent of the system
    # ANSI code page, which is GBK on zh-CN systems.
    $env:PYTHONUTF8 = '1'
}

function Start-AllServices {
    $preflight = Invoke-Preflight
    $nodePath = $preflight.node
    $venvPython = $preflight.venvPython

    Clear-ModelEnvironment

    $started = @{}

    Write-Step 'Starting agent (8001)'
    $started['agent'] = Start-ManagedProcess -Name 'agent' -Exe $venvPython `
        -Arguments @('-m', 'uvicorn', 'agent_service.app:app', '--host', '127.0.0.1', '--port', '8001') `
        -WorkingDirectory $ProjectRoot
    [void](Wait-Healthy -Name 'agent' -Url $HealthUrls['agent'])
    Write-Ok 'agent ready'

    Write-Step 'Starting backend (3002)'
    $env:PORT = '3002'
    $env:HOST = '127.0.0.1'
    $env:AGENT_SERVICE_URL = 'http://127.0.0.1:8001'
    $started['backend'] = Start-ManagedProcess -Name 'backend' -Exe $nodePath `
        -Arguments @((Join-Path $ProjectRoot 'backend\node_modules\ts-node\dist\bin.js'), 'src/index.ts') `
        -WorkingDirectory (Join-Path $ProjectRoot 'backend')
    Remove-Item Env:PORT, Env:HOST, Env:AGENT_SERVICE_URL -ErrorAction SilentlyContinue
    [void](Wait-Healthy -Name 'backend' -Url $HealthUrls['backend'])
    Write-Ok 'backend ready'

    Write-Step 'Starting frontend (5174)'
    $started['frontend'] = Start-ManagedProcess -Name 'frontend' -Exe $nodePath `
        -Arguments @((Join-Path $ProjectRoot 'frontend\node_modules\vite\bin\vite.js'),
                     '--host', '0.0.0.0', '--port', '5174', '--strictPort') `
        -WorkingDirectory (Join-Path $ProjectRoot 'frontend')
    [void](Wait-Healthy -Name 'frontend' -Url $HealthUrls['frontend'])
    Write-Ok 'frontend ready'

    Write-State $started

    $state = Get-Health $StudioHealthUrl
    if ($null -eq $state) {
        Write-Warn 'Services are up, but the frontend-to-backend proxy check failed.'
    } else {
        Write-Ok ('Model configuration: ' + [string]$state.model)
        if (-not $state.llmConfigured) {
            Write-Warn 'Model configuration is incomplete; AI generation is unavailable. Check .env.'
        }
        if (-not $state.rendererAvailable) {
            Write-Warn 'No usable LibreOffice renderer was found; PPTX/PDF export is unavailable.'
        }
    }

    Write-Host ''
    Write-Host 'Open: http://127.0.0.1:5174/' -ForegroundColor Green
    Write-Host "Logs: $RunDir"
    Write-Host 'Status: -Action status    Stop: -Action stop    Restart: -Action restart'
}

function Show-Status {
    $data = Read-State
    foreach ($name in @('agent', 'backend', 'frontend')) {
        $port = $Ports[$name]
        $managed = $false
        if (@($data.PSObject.Properties.Name) -contains $name) {
            $entry = $data.$name
            $managed = ((Get-Identity ([int]$entry.pid)) -eq $entry.startTime)
        }
        $state = if ($managed) { 'running (managed by this script)' }
                 elseif (Test-PortOpen $port) { 'port taken by another process' }
                 else { 'not running' }
        Write-Host ("{0,-9} {1}: {2}" -f $name, $port, $state)
    }
    $state = Get-Health $StudioHealthUrl
    if ($state) { Write-Host ('Model configuration: ' + [string]$state.model) }
}

# ---------------------------------------------------------------------------
switch ($Action) {
    'start'   { Start-AllServices }
    'stop'    { Stop-ManagedProcesses }
    'status'  { Show-Status }
    'restart' {
        Write-Step 'Stopping the running services'
        Stop-ManagedProcesses
        Start-AllServices
    }
}
