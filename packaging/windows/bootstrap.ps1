# work-buddy install bootstrap, invoked by the Inno Setup installer.
#
# Runs the uv sequence and provisioning inside the chosen HOME:
#   uv python install 3.11 -> uv venv -> uv pip install -e (CPU torch, retried)
#   -> wbuddy provision -> wbuddy autostart enable.
#
# The dependency download is the slow, failure-prone step (a few hundred MB over
# possibly-flaky networks), so it is retried with backoff; uv's cache makes each
# retry resumable. The whole script is idempotent: re-running repairs.
#
# Note: the parameter is $AppHome, not $Home ($HOME is a PowerShell automatic
# variable and must not be shadowed).

param(
    [Parameter(Mandatory = $true)][string]$AppHome,
    [Parameter(Mandatory = $true)][string]$Data,
    [Parameter(Mandatory = $true)][string]$Uv,
    [string]$VaultRoot = "",
    [string]$AnthropicKey = ""
)

$ErrorActionPreference = "Stop"
$venvPy = Join-Path $AppHome ".venv\Scripts\python.exe"

# The installer runs this hidden and does not surface a nonzero exit, so failure
# handling lives here: everything is transcribed to install.log in the DATA dir,
# and a failure raises a visible dialog instead of a silent "successful" finish.
New-Item -ItemType Directory -Force -Path $Data | Out-Null
$logPath = Join-Path $Data "install.log"
Start-Transcript -Path $logPath -Append | Out-Null

function Invoke-Step {
    param([string]$Desc, [scriptblock]$Body)
    Write-Host "==> $Desc"
    & $Body
    if ($LASTEXITCODE -ne 0) { throw "$Desc failed (exit $LASTEXITCODE)" }
}

trap {
    $msg = $_.Exception.Message
    try { Stop-Transcript | Out-Null } catch {}
    try {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show(
            "work-buddy setup did not complete:`n`n$msg`n`nDetails: $logPath`nRe-run the installer to resume (downloads are cached).",
            "work-buddy setup", "OK", "Error") | Out-Null
    } catch {}
    exit 1
}

# 1. A self-contained managed CPython 3.11 (not system python, not conda).
Invoke-Step "Installing Python 3.11" { & $Uv python install 3.11 }

# 2. A venv inside the HOME (co-located with the code; rebuilds with the checkout).
Invoke-Step "Creating the virtual environment" {
    & $Uv venv --python 3.11 (Join-Path $AppHome ".venv")
}

# 3. Dependencies + editable install of work-buddy. THE slow step (CPU torch).
#    Retry with backoff; uv's cache resumes partial downloads.
$maxAttempts = 3
for ($attempt = 1; ; $attempt++) {
    Write-Host "==> Downloading dependencies (attempt $attempt of $maxAttempts; this can take several minutes)"
    & $Uv pip install --python $venvPy --extra-index-url https://download.pytorch.org/whl/cpu -e $AppHome
    if ($LASTEXITCODE -eq 0) { break }
    if ($attempt -ge $maxAttempts) {
        throw "Dependency install failed after $attempt attempts. Re-run the installer to resume (downloads are cached)."
    }
    Start-Sleep -Seconds ([Math]::Pow(2, $attempt))
}

# 4. Provision: config, secrets, data-dir relocation, .mcp.json, bootstrap checks,
#    and start the sidecar. --home makes the target explicit.
$provArgs = @("-m", "work_buddy.cli", "provision", "--home", $AppHome, "--data-dir", $Data)
if ($VaultRoot)    { $provArgs += @("--vault-root", $VaultRoot) }
if ($AnthropicKey) { $provArgs += @("--anthropic-key", $AnthropicKey) }
Invoke-Step "Provisioning work-buddy" { & $venvPy @provArgs }

# 5. Register login auto-start (the WB-Sidecar scheduled task).
Invoke-Step "Registering login auto-start" { & $venvPy -m work_buddy.cli autostart enable }

Write-Host "==> work-buddy install complete."
Stop-Transcript | Out-Null
