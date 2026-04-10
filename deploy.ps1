# =============================================================================
# deploy.ps1  -  Deploy the QQ Bot adapter into hermes-agent (Windows)
#
# Usage:
#   .\deploy.ps1 [HermesAgentDir]
#
# If HermesAgentDir is not supplied, the script uses ~\.hermes\hermes-agent.
# =============================================================================

param(
    [string]$HermesAgentDir = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

# ---------------------------------------------------------------------------
# Resolve hermes-agent path
# ---------------------------------------------------------------------------
if ($HermesAgentDir -eq "") {
    $HermesAgentDir = Join-Path $env:USERPROFILE ".hermes\hermes-agent"
}

if (-not (Test-Path $HermesAgentDir -PathType Container)) {
    Write-Error "ERROR: hermes-agent directory not found: $HermesAgentDir"
    Write-Host "Usage: .\deploy.ps1 [C:\path\to\hermes-agent]"
    exit 1
}
$AgentDir = Resolve-Path $HermesAgentDir

# ---------------------------------------------------------------------------
# Find Python (prefer hermes-agent's venv)
# ---------------------------------------------------------------------------
$VenvPython  = Join-Path $AgentDir "venv\Scripts\python.exe"
$Venv2Python = Join-Path $AgentDir ".venv\Scripts\python.exe"

if (Test-Path $VenvPython) {
    $PythonCmd = $VenvPython
    Write-Host "OK  Using venv: $(Join-Path $AgentDir 'venv')"
} elseif (Test-Path $Venv2Python) {
    $PythonCmd = $Venv2Python
    Write-Host "OK  Using venv: $(Join-Path $AgentDir '.venv')"
} else {
    # Fall back to system Python
    $PythonCmd = (Get-Command python -ErrorAction SilentlyContinue)?.Source
    if (-not $PythonCmd) {
        $PythonCmd = (Get-Command python3 -ErrorAction SilentlyContinue)?.Source
    }
    if (-not $PythonCmd) {
        Write-Error "ERROR: Python not found. Please install Python 3.9+ and add it to PATH."
        exit 1
    }
    Write-Host "OK  Using system Python: $PythonCmd"
}

Write-Host ""
Write-Host "============================================================"
Write-Host "  Deploying QQ Bot adapter to: $AgentDir"
Write-Host "============================================================"
Write-Host ""

# ---------------------------------------------------------------------------
# Step 1: Install Python dependencies
# ---------------------------------------------------------------------------
Write-Host ">>> Step 1/3 - Installing Python dependencies..."

# Ensure pip is available
& $PythonCmd -m pip --version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  pip not found, bootstrapping with ensurepip..."
    & $PythonCmd -m ensurepip --upgrade 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "  ensurepip failed. Please install pip manually."
        exit 1
    }
    Write-Host "  [ok] pip bootstrapped"
}

Write-Host "  Checking required dependencies..."
$MissingPkgs = @()
foreach ($pkg in @("httpx", "websockets", "pyyaml")) {
    $importName = if ($pkg -eq "pyyaml") { "yaml" } else { $pkg }
    & $PythonCmd -c "import $importName" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        $MissingPkgs += $pkg
    }
}

if ($MissingPkgs.Count -eq 0) {
    Write-Host "  [ok] All dependencies already installed (httpx, websockets, pyyaml)"
} else {
    Write-Host "  Missing: $($MissingPkgs -join ', ')"
    Write-Host "  Installing $($MissingPkgs -join ' ')..."
    & $PythonCmd -m pip install --quiet @MissingPkgs
    if ($LASTEXITCODE -ne 0) {
        Write-Error "  ERROR: pip install failed. Please run manually:`n    $PythonCmd -m pip install $($MissingPkgs -join ' ')"
        exit 1
    }
    Write-Host "  [ok] Dependencies installed successfully"
}

# ---------------------------------------------------------------------------
# Step 2: Apply patches
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host ">>> Step 2/3 - Applying patches to hermes-agent..."
& $PythonCmd (Join-Path $ScriptDir "patch_hermes.py") $AgentDir
if ($LASTEXITCODE -ne 0) { exit 1 }

# ---------------------------------------------------------------------------
# Step 3: Validate
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host ">>> Step 3/3 - Validating patches..."

$QqbotFile = Join-Path $AgentDir "gateway\platforms\qqbot.py"
if (Test-Path $QqbotFile) {
    Write-Host "  [ok] gateway/platforms/qqbot.py exists"
} else {
    Write-Error "  ERROR: qqbot.py was not copied!"
    exit 1
}

$ConfigPy = Join-Path $AgentDir "gateway\config.py"
if (Select-String -Path $ConfigPy -Pattern 'QQBOT = "qqbot"' -Quiet) {
    Write-Host "  [ok] Platform.QQBOT in gateway/config.py"
} else {
    Write-Error "  ERROR: Platform.QQBOT not found in config.py"
    exit 1
}

$RunPy = Join-Path $AgentDir "gateway\run.py"
if (Select-String -Path $RunPy -Pattern 'Platform\.QQBOT' -Quiet) {
    Write-Host "  [ok] QQBotAdapter registered in gateway/run.py"
} else {
    Write-Error "  ERROR: QQBotAdapter not found in run.py"
    exit 1
}

# ---------------------------------------------------------------------------
# Configure credentials (interactive)
# ---------------------------------------------------------------------------
$ConfigFile = Join-Path (Split-Path $AgentDir -Parent) "config.yaml"
Write-Host ""
$answer = Read-Host ">>> Do you want to configure QQ credentials in $ConfigFile? [y/N]"
if ($answer -match '^[Yy]') {
    $AppId = ""
    while ($AppId.Trim() -eq "") {
        $AppId = Read-Host "  Enter QQ App ID"
    }
    $ClientSecret = Read-Host "  Enter QQ Client Secret" -AsSecureString
    $ClientSecretPlain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($ClientSecret)
    )
    while ($ClientSecretPlain.Trim() -eq "") {
        $ClientSecret = Read-Host "  Client Secret cannot be empty, enter QQ Client Secret" -AsSecureString
        $ClientSecretPlain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($ClientSecret)
        )
    }

    # Write credentials via Python (same logic as deploy.sh)
    $PyScript = @"
import sys
from pathlib import Path
config_path = Path(sys.argv[1])
app_id = sys.argv[2]
client_secret = sys.argv[3]
try:
    import yaml
except Exception:
    print('ERROR: PyYAML not found. Run: pip install pyyaml', file=sys.stderr)
    sys.exit(2)
if config_path.exists():
    raw = config_path.read_text(encoding='utf-8')
    data = yaml.safe_load(raw) if raw.strip() else {}
    if not isinstance(data, dict):
        data = {}
else:
    data = {}
platforms = data.setdefault('platforms', {})
qqbot = platforms.setdefault('qqbot', {})
qqbot['enabled'] = True
extra = qqbot.setdefault('extra', {})
extra['app_id'] = str(app_id)
extra['client_secret'] = str(client_secret)
extra.pop('appId', None)
extra.pop('clientSecret', None)
config_path.parent.mkdir(parents=True, exist_ok=True)
config_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding='utf-8')
print(f'  [ok] Credentials written to {config_path}')
"@
    & $PythonCmd -c $PyScript $ConfigFile $AppId $ClientSecretPlain
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "  Failed to write credentials automatically. Please set them manually (see below)."
    }
} else {
    Write-Host "  [skip] Credentials were not changed."
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "============================================================"
Write-Host "  Deployment complete!"
Write-Host "============================================================"
Write-Host ""
Write-Host "Credentials (choose one method):"
Write-Host ""
Write-Host "  Option A - Environment variables (add to your shell profile):"
Write-Host '    $env:QQBOT_APP_ID = "<your-app-id>"'
Write-Host '    $env:QQBOT_CLIENT_SECRET = "<your-client-secret>"'
Write-Host ""
Write-Host "  Option B - config.yaml (~\.hermes\config.yaml):"
Write-Host "    platforms:"
Write-Host "      qqbot:"
Write-Host "        enabled: true"
Write-Host "        extra:"
Write-Host "          app_id: '<your-app-id>'"
Write-Host "          client_secret: '<your-client-secret>'"
Write-Host ""
Write-Host "Then start hermes-agent normally."
Write-Host ""
