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

# Keep uv's data dir and managed Python under the per-user DATA dir (self-contained,
# and off the roaming profile). The Python version-link failure this used to chase
# is handled at the install step below (we bypass the link), not by relocation.
$uvDir = Join-Path $Data "uv"
$env:UV_DATA_DIR = $uvDir
$env:UV_PYTHON_INSTALL_DIR = Join-Path $uvDir "python"

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

# 1. Install a self-contained managed CPython 3.11 (not system python, not conda).
#    IMPORTANT: uv finishes by creating a "minor version link" (a directory junction
#    cpython-3.11-... -> cpython-3.11.15-...) purely for transparent patch upgrades.
#    That link step fails on common Windows setups and is NOT recoverable by retry:
#    when the installer runs with Windows' RedirectionGuard mitigation (Inno 6.7+
#    enables it by default and it is inherited by child processes) junction traversal
#    is blocked with os error 448; with the guard off, uv then reports a "missing
#    target directory" for the same link. The Python interpreter itself ALWAYS
#    extracts fine either way. So run the install best-effort and then locate the
#    real versioned python.exe directly, bypassing the broken link entirely.
Write-Host "==> Installing Python 3.11 (uv)"
# Best-effort. uv's final version-link and shim steps can warn or fail on Windows,
# and those surface on stderr. With ErrorActionPreference=Stop, even a uv WARNING on
# stderr would be promoted to a terminating error and abort us. So run this step
# with error handling relaxed, and gate real success on whether a versioned
# python.exe actually appears (checked next), NOT on uv's exit code or stderr.
& {
    $ErrorActionPreference = "Continue"
    & $Uv python install 3.11 2>&1 | ForEach-Object { Write-Host $_ }
}
$pyExe = Get-ChildItem (Join-Path $uvDir "python") -Recurse -Filter python.exe -Depth 2 -ErrorAction SilentlyContinue |
    Where-Object { $_.Directory.Name -like "cpython-3.11.*-windows-*" } |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $pyExe) {
    throw "Python 3.11 install produced no interpreter under $uvDir\python"
}
Write-Host "==> Using Python at $pyExe"

# 2. A venv inside the HOME, pointed straight at that python.exe (never the link).
#    --clear so a re-run (the advertised "resume" path) replaces a half-built venv.
Invoke-Step "Creating the virtual environment" -Retries 3 {
    & $Uv venv --clear --python $pyExe (Join-Path $AppHome ".venv")
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

# 5. Register login auto-start (the WB-Sidecar scheduled task). BEST-EFFORT: by
#    now work-buddy is installed and running (sidecar up, MCP wired), so failing to
#    register the login task must NOT fail the whole install. Warn and continue; a
#    separate marker records whether it succeeded so the finish page can mention it.
Write-Host "==> Registering login auto-start"
$autostartMarker = Join-Path $Data ".autostart-ok"
Remove-Item -Force -ErrorAction SilentlyContinue $autostartMarker
& {
    $ErrorActionPreference = "Continue"
    & $venvPy -m work_buddy.cli autostart enable 2>&1 | ForEach-Object { Write-Host $_ }
}
if ($LASTEXITCODE -eq 0) {
    New-Item -ItemType File -Force -Path $autostartMarker | Out-Null
} else {
    Write-Host "WARNING: could not register login auto-start. work-buddy is installed and running; it just will not restart automatically after a reboot. You can retry later with:  wbuddy autostart enable"
}

Write-Host "==> work-buddy install complete."
# Signal overall success to the installer (its finish page checks this marker).
New-Item -ItemType File -Force -Path $markerPath | Out-Null
Stop-Transcript | Out-Null
