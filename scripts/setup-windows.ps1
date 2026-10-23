#Requires -Version 5.1
<#
.SYNOPSIS
    Prepare the ppt_as runtime dependencies on Windows.

.DESCRIPTION
    Every install artifact and cache (Python virtualenv, pip cache, npm cache,
    pnpm store, temp files) is placed next to the project on its own drive.
    Nothing is written to the C: drive, and no packages are installed into the
    system-wide Python.

    NOTE: messages are intentionally ASCII-only so the script renders correctly
    under Windows PowerShell 5.1, which decodes BOM-less files using the system
    ANSI code page (GBK on zh-CN systems).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot,
    [switch]$SkipNodeInstall
)

# CmdletBinding makes this an advanced script, so param defaults are evaluated in
# the caller's scope where $PSScriptRoot is empty. Resolve it in the body instead.
if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ProjectRoot   = (Resolve-Path -LiteralPath $ProjectRoot).Path
$CacheDir      = Join-Path $ProjectRoot '.cache'
$VenvDir       = Join-Path $ProjectRoot '.venv'
$EnvFile       = Join-Path $ProjectRoot '.env'
$Requirements  = Join-Path $ProjectRoot 'agent_service\requirements.txt'

function Write-Step([string]$Text) { Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text)   { Write-Host "    [OK] $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "    [!]  $Text" -ForegroundColor Yellow }

# ---------------------------------------------------------------------------
# 1. Redirect every cache and temp directory onto the project drive
# ---------------------------------------------------------------------------
Write-Step 'Redirecting caches and temp dirs onto the project drive'
foreach ($name in @('pip', 'npm', 'pnpm-store', 'pnpm-home', 'tmp')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $CacheDir $name) | Out-Null
}
$env:PIP_CACHE_DIR     = Join-Path $CacheDir 'pip'
$env:npm_config_cache = Join-Path $CacheDir 'npm'
$env:PNPM_HOME        = Join-Path $CacheDir 'pnpm-home'
$env:PNPM_STORE_DIR   = Join-Path $CacheDir 'pnpm-store'
$env:TEMP             = Join-Path $CacheDir 'tmp'
$env:TMP              = Join-Path $CacheDir 'tmp'
# Husky's git hooks are unreliable on Windows; skipping them does not affect runtime.
$env:HUSKY            = '0'
Write-Ok "Cache directory: $CacheDir"

# ---------------------------------------------------------------------------
# 2. Resolve a Python interpreter (3.12+)
# ---------------------------------------------------------------------------
Write-Step 'Checking Python'

$script:PyExe    = $null
$script:PyPrefix = @()

# The pinned requirements need 3.12 or newer: numpy 2.5.3 declares
# Requires-Python >=3.12, so 3.11 cannot be the base of the virtualenv.
$MinimumPython = 312

function Test-PythonCandidate([string]$Exe, [string[]]$Prefix) {
    try {
        $out = & $Exe @Prefix -c "import sys; print(sys.version_info[0]*100 + sys.version_info[1])" 2>$null
    } catch {
        return $false
    }
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $false }
    $code = [int]($out | Select-Object -First 1)
    if ($code -ge $MinimumPython) {
        $script:PyExe    = $Exe
        $script:PyPrefix = $Prefix
        return $true
    }
    return $false
}

# Collect every interpreter the py launcher knows about, then the one on PATH.
# The launcher keeps registrations for interpreters that were uninstalled, so a
# candidate is only accepted after it actually runs and reports its version.
$candidates = New-Object System.Collections.ArrayList
if (Get-Command py.exe -ErrorAction SilentlyContinue) {
    foreach ($line in @(& py.exe -0p 2>$null)) {
        if ("$line" -match '([A-Za-z]:\\.*python\.exe)\s*$') {
            [void]$candidates.Add(@($Matches[1], @()))
        }
    }
}
$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $pythonCommand) { $pythonCommand = Get-Command python -ErrorAction SilentlyContinue }
if ($pythonCommand) { [void]$candidates.Add(@($pythonCommand.Source, @())) }

foreach ($candidate in $candidates) {
    if (Test-PythonCandidate $candidate[0] $candidate[1]) { break }
}
if (-not $script:PyExe) {
    throw "Python $MinimumPython or newer not found. Install Python 3.12+ (tick `"Add python.exe to PATH`") or register it with the py launcher."
}
$pyVersion = (& $script:PyExe @($script:PyPrefix) -c "import sys; print('%d.%d.%d' % sys.version_info[:3])")
Write-Ok "Python $pyVersion  ($($script:PyExe) $($script:PyPrefix -join ' '))"

# ---------------------------------------------------------------------------
# 3. Create the virtual environment
# ---------------------------------------------------------------------------
Write-Step 'Creating the Python virtual environment'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
if (Test-Path $VenvPython) {
    Write-Ok 'Already exists, skipping'
} else {
    & $script:PyExe @($script:PyPrefix) -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the virtual environment: $VenvDir" }
    Write-Ok "Created: $VenvDir"
}
if (-not (Test-Path $VenvPython)) { throw "Virtualenv interpreter missing: $VenvPython" }

# ---------------------------------------------------------------------------
# 4. Install Python dependencies (into the venv only)
# ---------------------------------------------------------------------------
Write-Step 'Installing Python dependencies into the virtual environment'
& $VenvPython -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw 'Failed to upgrade pip' }
& $VenvPython -m pip install -r $Requirements
if ($LASTEXITCODE -ne 0) { throw 'Failed to install the Python dependencies' }
Write-Ok "Installed (pip cache: $env:PIP_CACHE_DIR)"

# ---------------------------------------------------------------------------
# 5. Node.js and pnpm
# ---------------------------------------------------------------------------
Write-Step 'Checking Node.js and pnpm'

$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
if (-not $nodeCommand) { $nodeCommand = Get-Command node -ErrorAction SilentlyContinue }
if (-not $nodeCommand) { throw 'Node.js not found. Install Node.js 22 LTS.' }
$nodeVersion = (& node --version).Trim() -replace '^v', ''
$nodeMajor = [int]($nodeVersion -split '\.')[0]
if ($nodeMajor -lt 22) { throw "Node.js $nodeVersion is too old; 22 or newer is required." }
$nodePath = $nodeCommand.Source
Write-Ok "Node.js $nodeVersion"

$PnpmHome  = Join-Path $CacheDir 'pnpm-home'
$PnpmLocal = Join-Path $PnpmHome 'node_modules\.bin\pnpm.cmd'
$pnpmCommand = Get-Command pnpm.cmd -ErrorAction SilentlyContinue
if (-not $pnpmCommand) { $pnpmCommand = Get-Command pnpm -ErrorAction SilentlyContinue }
if ($pnpmCommand) {
    $PnpmExe = $pnpmCommand.Source
} else {
    Write-Warn 'pnpm not on PATH; installing it into .cache\pnpm-home instead'
    & npm install --prefix $PnpmHome --no-fund --no-audit pnpm@latest
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $PnpmLocal)) { throw 'Failed to install pnpm' }
    $PnpmExe = $PnpmLocal
}
$env:PNPM_HOME = $PnpmHome
$env:PATH = "$PnpmHome;$PnpmHome\node_modules\.bin;$env:PATH"
Write-Ok "pnpm $((& $PnpmExe --version).Trim())  ($PnpmExe)"

# ---------------------------------------------------------------------------
# 6. Node dependencies and the PPTD runtime
# ---------------------------------------------------------------------------
if (-not $SkipNodeInstall) {
    Write-Step 'Installing Node dependencies (pnpm workspace)'
    # The default pnpm reporter writes an ANSI window-title sequence. When the
    # output is redirected the PowerShell console host tries to apply it to the
    # window title and fails with a terminating HostException, so the append-only
    # reporter is used instead.
    try {
        & $PnpmExe install --store-dir (Join-Path $CacheDir 'pnpm-store') --reporter=append-only
    } catch [System.Management.Automation.HostException] {
        Write-Warn "pnpm reported a console host error: $($_.Exception.Message)"
    }
    if ($LASTEXITCODE -ne 0) { throw 'pnpm install failed' }
    Write-Ok 'node_modules is ready'

    Write-Step 'Building the PPTD browser runtime'
    try {
        & $PnpmExe build:pptd --reporter=append-only
    } catch [System.Management.Automation.HostException] {
        Write-Warn "pnpm reported a console host error: $($_.Exception.Message)"
    }
    if ($LASTEXITCODE -ne 0) { throw 'pnpm build:pptd failed' }
    if (-not (Test-Path (Join-Path $ProjectRoot 'packages\pptd-runtime\build\pptd-runtime.js'))) {
        throw 'PPTD runtime artifact missing: packages\pptd-runtime\build\pptd-runtime.js'
    }
    Write-Ok 'PPTD runtime built'
}

# ---------------------------------------------------------------------------
# 7. .env
# ---------------------------------------------------------------------------
Write-Step 'Preparing .env'
if (Test-Path $EnvFile) {
    Write-Ok 'Already exists, left untouched'
} else {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot '.env.example') -Destination $EnvFile
    Write-Ok "Created from .env.example: $EnvFile"
    Write-Warn 'Edit .env and fill in the model configuration (PPT_LLM_PROVIDER / PPT_LLM_MODEL / OPENAI_API_KEY / OPENAI_BASE_URL).'
}

# ---------------------------------------------------------------------------
# 8. Optional components
# ---------------------------------------------------------------------------
Write-Step 'Checking optional components'
$soffice = Get-Command soffice.exe -ErrorAction SilentlyContinue
if (-not $soffice) {
    foreach ($candidate in @(
        'C:\Program Files\LibreOffice\program\soffice.exe',
        'C:\Program Files (x86)\LibreOffice\program\soffice.exe')) {
        if (Test-Path $candidate) { $soffice = $candidate; break }
    }
}
if ($soffice) {
    Write-Ok "LibreOffice: $soffice (PPTX/PDF export available)"
} else {
    Write-Warn 'LibreOffice not found. PPTX/PDF export will not work; install LibreOffice or use the Docker renderer.'
}

Write-Host "`nDone. Next: powershell -ExecutionPolicy Bypass -File scripts\services-windows.ps1 -Action start" -ForegroundColor Green
