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

# Install the managed Python INSIDE the HOME, not uv's shared per-user dir. A
# fresh install-local dir has no pre-existing version-link reparse points, which
# on Windows otherwise fail permanently (uv tries to recreate them and Windows
# rejects re-traversing an existing reparse point: "untrusted mount point", os
# error 448). It also keeps Python self-contained, so uninstall removes it too.
$env:UV_PYTHON_INSTALL_DIR = Join-Path $AppHome ".uv\python"

# The installer runs this hidden and Inno does not treat a nonzero exit as fatal.
# So success is signalled by a marker file: the installer aborts if it is absent
# after this runs. Clear any stale marker up front; everything is transcribed to
# install.log for diagnosis.
New-Item -ItemType Directory -Force -Path $Data | Out-Null
$markerPath = Join-Path $Data ".install-ok"
Remove-Item -Force -ErrorAction SilentlyContinue $markerPath
$logPath = Join-Path $Data "install.log"
Start-Transcript -Path $logPath -Append | Out-Null

function Invoke-Step {
    param([string]$Desc, [scriptblock]$Body, [int]$Retries = 1)
    for ($attempt = 1; ; $attempt++) {
        if ($Retries -gt 1) { Write-Host "==> $Desc (attempt $attempt of $Retries)" }
        else { Write-Host "==> $Desc" }
        & $Body
        if ($LASTEXITCODE -eq 0) { return }
        if ($attempt -ge $Retries) {
            $suffix = if ($Retries -gt 1) { " after $attempt attempts" } else { "" }
            throw "$Desc failed (exit $LASTEXITCODE)$suffix"
        }
        # Transient failure: a flaky download, or antivirus briefly blocking uv's
        # reparse-point creation (Windows "untrusted mount point", os error 448).
        # Back off and retry; uv's cache makes retries resumable.
        Start-Sleep -Seconds ([Math]::Pow(2, $attempt))
    }
}

trap {
    # No dialog here: the marker is never written, so the installer detects the
    # failure and shows a single error message pointing at the log. Just record
    # the reason and exit nonzero.
    Write-Host "ERROR: $($_.Exception.Message)"
    try { Stop-Transcript | Out-Null } catch {}
    exit 1
}

# All three uv steps are retried: each touches the network and/or creates
# reparse points that antivirus can transiently block on a fresh path.

# 1. A self-contained managed CPython 3.11 (not system python, not conda).
Invoke-Step "Installing Python 3.11" -Retries 3 { & $Uv python install 3.11 }

# 2. A venv inside the HOME (co-located with the code; rebuilds with the checkout).
#    --clear so a re-run (the advertised "resume" path) replaces a half-built
#    venv instead of erroring that one already exists.
Invoke-Step "Creating the virtual environment" -Retries 3 {
    & $Uv venv --clear --python 3.11 (Join-Path $AppHome ".venv")
}

# 3. Dependencies + editable install of work-buddy. THE slow step (CPU torch,
#    a few hundred MB). uv's cache resumes partial downloads between attempts.
#    --index-strategy unsafe-best-match: the CPU-torch index also hosts stale
#    copies of small shared packages (e.g. charset-normalizer); without this,
#    uv's default "first index wins" rule pins those stale versions and
#    resolution fails. Safe here: both indexes (PyPI + official PyTorch) are
#    trusted, so there is no dependency-confusion exposure.
Invoke-Step "Downloading dependencies (this can take several minutes)" -Retries 3 {
    & $Uv pip install --python $venvPy --index-strategy unsafe-best-match --extra-index-url https://download.pytorch.org/whl/cpu -e $AppHome
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
# Signal success to the installer (its [Code] guard aborts if this is missing).
New-Item -ItemType File -Force -Path $markerPath | Out-Null
Stop-Transcript | Out-Null
